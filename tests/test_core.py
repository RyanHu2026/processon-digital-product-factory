import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import app


class CoreTests(unittest.TestCase):
    def test_validate_and_quality(self):
        data = app.validate_map(app.demo_map({"title": "测试主题", "category": "测试", "audience": "学习者", "difficulty": "标准"}))
        report = app.quality_report(data)
        self.assertGreater(report["node_count"], 30)
        self.assertGreaterEqual(report["max_depth"], 3)
        self.assertIn("title", data)

    def test_reject_empty_node(self):
        with self.assertRaises(ValueError):
            app.validate_map({"title": "x", "category": "x", "audience": "x", "difficulty": "x", "branches": [{"name": "", "children": []}]})

    def test_clean_model_json_wrappers(self):
        expected = {"title": "主题", "branches": []}
        responses = [
            '{"title":"主题","branches":[]}',
            '```json\n{"title":"主题","branches":[]}\n```',
            '下面是结果：\n{"title":"主题","branches":[]}\n以上。',
            '<think>先整理结构 {暂不输出}</think>{"title":"主题","branches":[]}',
        ]
        for response in responses:
            self.assertEqual(json.loads(app.clean_json_text(response)), expected)

    def test_clean_model_json_rejects_invalid_response(self):
        with self.assertRaises(ValueError):
            app.clean_json_text("模型没有返回 JSON")

    def test_model_prompt_does_not_force_old_learning_template(self):
        prompt = app.model_prompt({"title": "计量营销学", "category": "专业课程", "audience": "学习者", "difficulty": "标准"})
        self.assertNotIn("围绕主题建立清晰边界", prompt)
        self.assertNotIn("能否用自己的话解释并举例", prompt)
        self.assertIn("图形类型不是装饰字段", prompt)
        self.assertIn("本次图形类型：思维导图", prompt)
        advanced_prompt = app.model_prompt({"title": "统计学", "category": "课程", "audience": "学习者", "difficulty": "进阶"})
        self.assertIn("节点数量控制在 60-240 个", advanced_prompt)

    def test_batch_id_contains_time_and_title(self):
        batch_id = app.make_batch_id("市场营销学知识导图")
        self.assertIn("市场营销学知识导图", batch_id)
        self.assertRegex(batch_id, r"^\d{8}-\d{6}-市场营销学知识导图-")

    def test_chart_types_have_distinct_layouts_and_prompts(self):
        expected = {
            "思维导图": "org.xmind.ui.map.clockwise",
            "流程图": "org.xmind.ui.logic.right",
            "鱼骨图": "org.xmind.ui.fishbone.rightHeaded",
            "树状结构图": "org.xmind.ui.tree.right",
            "双边思维导图": "org.xmind.ui.map",
            "组织结构图": "org.xmind.ui.org-chart.down",
        }
        for chart_type, structure_class in expected.items():
            self.assertEqual(app.graph_type_config(chart_type)["structure_class"], structure_class)
            prompt = app.model_prompt({"title": "测试", "category": "测试", "audience": "学习者", "difficulty": "标准", "chart_type": chart_type})
            self.assertIn(f"本次图形类型：{chart_type}", prompt)
            self.assertIn(app.graph_type_config(chart_type)["rules"], prompt)
        with tempfile.TemporaryDirectory(dir=app.ROOT) as temp:
            for chart_type in ("思维导图", "双边思维导图"):
                path = app.write_xmind(Path(temp), chart_type, [{"name": "一侧", "children": []}, {"name": "另一侧", "children": []}], chart_type)
                with zipfile.ZipFile(path) as archive:
                    root = ET.fromstring(archive.read("content.xml"))
                topic = root.find(".//{urn:xmind:xmap:xmlns:content:2.0}topic")
                self.assertEqual(topic.attrib["structure-class"], app.graph_type_config(chart_type)["structure_class"])

    def test_pos_layouts_are_native_and_not_single_sided_xmind(self):
        sample = [{"name": "第一分支", "children": [{"name": "步骤一", "children": []}]}, {"name": "第二分支", "children": [{"name": "步骤二", "children": []}]}]
        with tempfile.TemporaryDirectory(dir=app.ROOT) as temp:
            for chart_type in app.GRAPH_TYPES:
                path = app.write_pos(Path(temp), chart_type, sample, chart_type)
                self.assertTrue(app.verify_pos(path))
                data = json.loads(path.read_text(encoding="utf-8"))
                elements = data["diagram"]["elements"]["elements"].values()
                self.assertTrue(any(element.get("name") == "linker" for element in elements))
                if chart_type == "流程图":
                    self.assertTrue(any(element.get("name") == "terminator" for element in elements))

    def test_difficulty_controls_node_budget(self):
        data = {"title": "统计学", "category": "课程", "audience": "学习者", "difficulty": "进阶",
                "branches": [{"name": "知识体系", "children": [{"name": f"知识点{i}", "children": []} for i in range(192)]}]}
        with self.assertRaises(ValueError):
            app.validate_map(data, "标准")
        validated = app.validate_map(data, "进阶")
        self.assertEqual(validated["_node_count"], 193)

    def test_standard_overflow_is_trimmed_instead_of_failing_batch(self):
        branches = [{"name": f"一级分支{i}", "children": [{"name": f"节点{i}-{j}", "children": []} for j in range(18)]} for i in range(8)]
        data = {"title": "乐理入门", "category": "课程", "audience": "学习者", "difficulty": "标准", "branches": branches, "quality_notes": []}
        original_count = app.walk_nodes(data["branches"])[0]
        self.assertEqual(original_count, 152)
        normalized = app.normalize_map_budget(data, "标准")
        validated = app.validate_map(normalized, "标准")
        self.assertEqual(validated["_node_count"], 140)
        self.assertEqual(len(validated["branches"]), 8)
        self.assertEqual(validated["_trimmed_nodes"], 12)
        self.assertTrue(any("自动裁剪" in note for note in validated["quality_notes"]))

    def test_standard_five_levels_are_flattened_to_four(self):
        deep = {"name": "一级", "children": [{"name": "二级", "children": [{"name": "三级", "children": [{"name": "四级", "children": [{"name": "五级", "children": []}]}]}]}]}
        data = {"title": "语音学", "category": "课程", "audience": "学习者", "difficulty": "标准", "branches": [deep], "quality_notes": []}
        normalized = app.normalize_map_budget(data, "标准")
        validated = app.validate_map(normalized, "标准")
        self.assertEqual(validated["_max_depth"], 4)
        self.assertTrue(any("自动压平" in note for note in validated["quality_notes"]))

    def test_non_mindmap_exports_pos_without_xmind(self):
        with tempfile.TemporaryDirectory(dir=app.ROOT) as temp:
            original = app.OUTPUTS
            app.OUTPUTS = Path(temp)
            try:
                topic = {"title": "语音学树状结构", "category": "课程", "audience": "学习者", "difficulty": "标准", "chart_type": "树状结构图"}
                data = app.validate_map(app.demo_map(topic))
                quality = app.quality_report(data)
                _, pos_rel, xmind_rel = app.write_outputs(topic, data, quality, "tree-batch")
                folder = Path(temp) / "tree-batch" / "语音学树状结构"
                self.assertTrue((app.ROOT / pos_rel).exists())
                self.assertEqual(xmind_rel, "")
                self.assertFalse(list(folder.glob("*.xmind")))
            finally:
                app.OUTPUTS = original

    def test_export_files_are_real_and_parseable(self):
        with tempfile.TemporaryDirectory(dir=app.ROOT) as temp:
            original = app.OUTPUTS
            app.OUTPUTS = Path(temp)
            try:
                topic = {"title": "中文：特殊/文件名?", "category": "测试", "audience": "学习者", "difficulty": "标准"}
                data = app.validate_map(app.demo_map(topic))
                quality = app.quality_report(data)
                app.write_outputs(topic, data, quality, "test-batch")
                folder_path = Path(temp) / "test-batch" / "中文：特殊_文件名_"
                self.assertFalse(list(folder_path.glob("*.opml")))
                self.assertFalse(list(folder_path.glob("*.txt")))
                xmind_path = next(folder_path.glob("*.xmind"))
                with zipfile.ZipFile(xmind_path) as archive:
                    self.assertEqual(set(archive.namelist()), {"content.xml", "styles.xml", "meta.xml", "META-INF/manifest.xml"})
                    content = ET.fromstring(archive.read("content.xml"))
                    topic = content.find(".//{urn:xmind:xmap:xmlns:content:2.0}topic")
                    self.assertEqual(topic.attrib["structure-class"], "org.xmind.ui.map.clockwise")
                    ET.fromstring(archive.read("META-INF/manifest.xml"))
                pos_path = next(folder_path.glob("*.pos"))
                pos_data = json.loads(pos_path.read_text(encoding="utf-8"))
                self.assertEqual(pos_data["meta"]["type"], "ProcessOn Schema File")
                self.assertTrue(app.verify_pos(pos_path))
                self.assertTrue((folder_path / "content.json").exists())
            finally:
                app.OUTPUTS = original


if __name__ == "__main__":
    unittest.main()
