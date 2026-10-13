import json
import tempfile
import unittest
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
                opml_path = next(folder_path.glob("*.opml"))
                txt_path = next(folder_path.glob("*.txt"))
                ET.parse(opml_path)
                self.assertTrue(txt_path.read_text(encoding="utf-8").startswith("中文：特殊"))
                self.assertTrue((folder_path / "content.json").exists())
            finally:
                app.OUTPUTS = original


if __name__ == "__main__":
    unittest.main()
