from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUTPUTS = DATA / "outputs"
STATIC = ROOT / "static"
DB_PATH = DATA / "factory.sqlite3"
PROMPT_PATH = ROOT / "prompts" / "mindmap_system.txt"
DB_LOCK = threading.RLock()
MODEL_SESSIONS: dict[str, dict] = {}
TASKS: dict[str, dict] = {}
TASK_LOCK = threading.RLock()


def now():
    return datetime.now().isoformat(timespec="seconds")


def db():
    DATA.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with DB_LOCK, db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS topics (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL,
          category TEXT NOT NULL DEFAULT '未分类',
          audience TEXT NOT NULL DEFAULT '个人学习者',
          difficulty TEXT NOT NULL DEFAULT '标准',
          status TEXT NOT NULL DEFAULT '待生成',
          batch_id TEXT,
          result_path TEXT,
          quality_score INTEGER,
          error TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS batches (
          id TEXT PRIMARY KEY,
          total INTEGER NOT NULL,
          completed INTEGER NOT NULL DEFAULT 0,
          failed INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT '排队中',
          model_name TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        """)


def json_response(handler, payload, status=200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    return json.loads(handler.rfile.read(length) or b"{}")


def slug(text):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).strip(" .")
    return value[:80] or "untitled"


def unique_path(directory, name, suffix):
    directory.mkdir(parents=True, exist_ok=True)
    base = directory / f"{slug(name)}{suffix}"
    if not base.exists():
        return base
    for index in range(2, 1000):
        candidate = directory / f"{slug(name)}_{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError("无法创建不重复的文件名")


def clean_json_text(text):
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.I | re.S)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start >= 0 and end > start else text


def walk_nodes(branches, depth=1):
    count = 0
    max_depth = depth
    names = []
    for node in branches or []:
        if not isinstance(node, dict) or not str(node.get("name", "")).strip():
            raise ValueError("存在空节点或非法节点")
        name = str(node["name"]).strip()
        if len(name) > 80:
            raise ValueError(f"节点过长：{name[:20]}...")
        count += 1
        names.append(name.casefold())
        children = node.get("children", [])
        if not isinstance(children, list):
            raise ValueError("children 必须是数组")
        child_count, child_depth, child_names = walk_nodes(children, depth + 1)
        count += child_count
        max_depth = max(max_depth, child_depth)
        names.extend(child_names)
    return count, max_depth, names


def validate_map(data):
    required = ["title", "category", "audience", "difficulty", "branches"]
    missing = [key for key in required if not data.get(key)]
    if missing:
        raise ValueError("缺少字段：" + ", ".join(missing))
    if not isinstance(data["branches"], list) or not data["branches"]:
        raise ValueError("branches 必须包含至少一个一级分支")
    count, depth, names = walk_nodes(data["branches"])
    if count > 120:
        raise ValueError(f"节点数量 {count} 超过 120 个上限")
    if depth > 4:
        raise ValueError(f"层级 {depth} 超过 4 层上限")
    duplicates = sorted({name for name in names if names.count(name) > 1})
    data["_node_count"] = count
    data["_max_depth"] = depth
    data["_duplicates"] = duplicates[:20]
    return data


def demo_map(topic):
    category = topic["category"]
    audience = topic["audience"]
    title = topic["title"]
    stems = ["核心概念", "知识结构", "应用方法", "案例场景", "常见误区", "复习行动"]
    branches = []
    for stem in stems:
        branches.append({"name": stem, "children": [
            {"name": f"{stem}的定义", "children": [{"name": f"围绕“{title}”建立清晰边界", "children": []}]},
            {"name": f"{stem}的要点", "children": [{"name": f"结合{audience}的使用目标拆解", "children": []}]},
            {"name": f"{stem}的检查问题", "children": [{"name": "能否用自己的话解释并举例", "children": []}]},
        ]})
    return {"title": title, "category": category, "audience": audience, "difficulty": topic["difficulty"],
            "branches": branches, "quality_notes": ["演示模式生成，正式发布前需人工核验事实和参考资料"],
            "sources_to_verify": ["请根据课程大纲、官方资料或权威教材补充核验"]}


def model_prompt(topic):
    system = PROMPT_PATH.read_text(encoding="utf-8") if PROMPT_PATH.exists() else "只输出合法 JSON。"
    return system + "\n\n本次主题 JSON：" + json.dumps(topic, ensure_ascii=False)


def call_model(topic, config):
    body = {
        "model": config["model"],
        "messages": [{"role": "user", "content": model_prompt(topic)}],
        "temperature": float(config.get("temperature", 0.4)),
        "max_tokens": int(config.get("max_tokens", 6000)),
    }
    url = config["base_url"].rstrip("/") + "/chat/completions"
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + config["api_key"]
    }, method="POST")
    timeout = max(10, int(config.get("timeout", 90)))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("模型返回中缺少 choices[0].message.content") from exc
    return json.loads(clean_json_text(content))


def generate_content(topic, config):
    if not config or config.get("demo_mode"):
        return demo_map(topic), "演示模式"
    last_error = None
    for attempt in range(int(config.get("retries", 1)) + 1):
        try:
            return call_model(topic, config), config.get("model", "未命名模型")
        except Exception as exc:
            last_error = exc
            time.sleep(0.4 * (attempt + 1))
    raise ValueError(f"模型生成失败：{last_error}")


def quality_report(data):
    count, depth, names = walk_nodes(data["branches"])
    branches = data["branches"]
    non_empty = sum(bool(str(node.get("name", "")).strip()) for node in branches)
    duplicate_count = len([name for name in set(names) if names.count(name) > 1])
    long_count = sum(1 for name in names if len(name) > 45)
    balance = max((len(branch.get("children", [])) for branch in branches), default=0) - min((len(branch.get("children", [])) for branch in branches), default=0)
    score = 25 + min(25, round(count / 3)) + (20 if count >= 30 else round(count / 2)) + (15 if depth >= 3 else 8) + max(0, 15 - duplicate_count * 4 - long_count)
    score = max(0, min(100, score))
    issues = []
    if count < 30: issues.append("有效节点少于建议的 30 个")
    if depth < 3: issues.append("层级较浅，建议补充三级节点")
    if duplicate_count: issues.append(f"检测到 {duplicate_count} 组重复节点")
    if long_count: issues.append(f"检测到 {long_count} 个可能过长的节点")
    if balance > 3: issues.append("一级分支数量不均衡")
    if data.get("sources_to_verify"): issues.append("存在需要人工核验的参考资料说明")
    return {"score": score, "node_count": count, "max_depth": depth, "empty_root_nodes": len(branches) - non_empty, "issues": issues, "duplicates": data.get("_duplicates", [])}


def publish_materials(data, quality):
    title = data["title"]
    keywords = [data["category"], data["audience"], title, "思维导图", "知识框架"]
    return {"suggested_title": title + "｜知识框架与复习行动地图", "alternative_titles": [title + "核心知识结构", title + "入门到实践思维导图"],
            "description": f"面向{data['audience']}的{data['category']}主题思维导图，包含核心概念、结构化要点、应用场景与复习行动。内容需结合个人学习目标进行核验和补充。",
            "audience": data["audience"], "scenarios": ["课程复习", "快速建立知识框架", "考前查漏补缺"],
            "highlights": [f"包含 {quality['node_count']} 个有效节点", f"最高 {quality['max_depth']} 层结构", "生成后可继续人工修改"],
            "keywords": list(dict.fromkeys(keywords)), "category_suggestion": data["category"],
            "price_reference": "待人工定价：需结合同类商品、内容深度和平台表现判断，不代表实际收益。",
            "checklist": ["核验事实和参考资料", "检查导入后的布局", "删除个人隐私和课程内部资料", "确认标题和简介没有夸大承诺"]}


def write_outputs(topic, data, quality, batch_id):
    folder = OUTPUTS / batch_id / slug(topic["title"])
    folder.mkdir(parents=True, exist_ok=True)
    data = {key: value for key, value in data.items() if not key.startswith("_")}
    raw_path = folder / "content.json"
    raw_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    title = data["title"]
    opml_path = unique_path(folder, title, ".opml")
    root = ET.Element("opml", version="2.0")
    ET.SubElement(root, "head").append(ET.Element("title"))
    root.find("head/title").text = title
    body = ET.SubElement(root, "body")
    ET.SubElement(body, "outline", text=title, _note="")
    root_outline = body[0]
    for branch in data["branches"]:
        branch_el = ET.SubElement(root_outline, "outline", text=branch["name"])
        for child in branch.get("children", []):
            child_el = ET.SubElement(branch_el, "outline", text=child["name"])
            for leaf in child.get("children", []):
                ET.SubElement(child_el, "outline", text=leaf["name"])
    ET.ElementTree(root).write(opml_path, encoding="utf-8", xml_declaration=True)
    txt_path = unique_path(folder, title, ".txt")
    lines = [title]
    def add(nodes, level):
        for node in nodes:
            lines.append("  " * level + "- " + node["name"])
            add(node.get("children", []), level + 1)
    add(data["branches"], 1)
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    quality_path = folder / "quality-report.json"
    quality_path.write_text(json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")
    materials = publish_materials(data, quality)
    (folder / "publish-materials.json").write_text(json.dumps(materials, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(folder.relative_to(ROOT)), str(opml_path.relative_to(ROOT)), str(txt_path.relative_to(ROOT))


def insert_topics(items, batch_id=None):
    created = []
    with DB_LOCK, db() as conn:
        for item in items:
            title = str(item.get("title", "")).strip()
            if not title: continue
            created.append(conn.execute("INSERT INTO topics(title,category,audience,difficulty,batch_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (title, item.get("category", "未分类"), item.get("audience", "个人学习者"), item.get("difficulty", "标准"), batch_id, now(), now())).lastrowid)
    return created


def list_topics():
    with DB_LOCK, db() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM topics ORDER BY id DESC LIMIT 500")]


def run_batch(batch_id, topic_ids, config):
    with DB_LOCK, db() as conn:
        conn.execute("UPDATE batches SET status=?, model_name=?, updated_at=? WHERE id=?", ("生成中", config.get("model", "演示模式"), now(), batch_id))
    for topic_id in topic_ids:
        with TASK_LOCK:
            if TASKS.get(batch_id, {}).get("cancelled"): break
        with DB_LOCK, db() as conn:
            row = conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
            conn.execute("UPDATE topics SET status=?, updated_at=? WHERE id=?", ("生成中", now(), topic_id))
        if not row: continue
        try:
            topic = dict(row)
            data, model_name = generate_content(topic, config)
            data = validate_map(data)
            quality = quality_report(data)
            paths = write_outputs(topic, data, quality, batch_id)
            with DB_LOCK, db() as conn:
                conn.execute("UPDATE topics SET status=?,result_path=?,quality_score=?,error=?,updated_at=? WHERE id=?", ("待审核", paths[0], quality["score"], "", now(), topic_id))
                conn.execute("UPDATE batches SET completed=completed+1,updated_at=? WHERE id=?", (now(), batch_id))
        except Exception as exc:
            with DB_LOCK, db() as conn:
                conn.execute("UPDATE topics SET status=?,error=?,updated_at=? WHERE id=?", ("失败", str(exc), now(), topic_id))
                conn.execute("UPDATE batches SET failed=failed+1,updated_at=? WHERE id=?", (now(), batch_id))
    with DB_LOCK, db() as conn:
        batch = conn.execute("SELECT total,completed,failed FROM batches WHERE id=?", (batch_id,)).fetchone()
        status = "已取消" if TASKS.get(batch_id, {}).get("cancelled") else "已完成"
        conn.execute("UPDATE batches SET status=?,updated_at=? WHERE id=?", (status, now(), batch_id))
    with TASK_LOCK:
        TASKS.pop(batch_id, None)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def send_file(self, path, content_type):
        data = Path(path).read_bytes()
        self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            with DB_LOCK, db() as conn:
                batches = [dict(row) for row in conn.execute("SELECT * FROM batches ORDER BY created_at DESC LIMIT 20")]
            return json_response(self, {"topics": list_topics(), "batches": batches, "output_dir": str(OUTPUTS)})
        if parsed.path == "/api/download":
            rel = parse_qs(parsed.query).get("path", [""])[0]
            candidate = (ROOT / rel).resolve()
            if ROOT not in candidate.parents or not candidate.is_file(): return json_response(self, {"error": "文件不存在"}, 404)
            return self.send_file(candidate, "application/octet-stream")
        if parsed.path == "/": return self.send_file(STATIC / "index.html", "text/html; charset=utf-8")
        static_path = (STATIC / parsed.path.lstrip("/")).resolve()
        if STATIC in static_path.parents and static_path.is_file():
            return self.send_file(static_path, "text/javascript; charset=utf-8" if static_path.suffix == ".js" else "text/css; charset=utf-8")
        return json_response(self, {"error": "Not found"}, 404)
    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            payload = read_json(self)
            if parsed.path == "/api/topics":
                ids = insert_topics(payload.get("topics", [])); return json_response(self, {"ids": ids})
            if parsed.path == "/api/models/session":
                session_id = uuid.uuid4().hex
                config = payload.get("config", {})
                if not config.get("demo_mode") and (not config.get("base_url") or not config.get("model") or not config.get("api_key")):
                    return json_response(self, {"error": "请填写 API 地址、API Key 和模型 ID，或启用演示模式"}, 400)
                MODEL_SESSIONS[session_id] = config; return json_response(self, {"session_id": session_id, "saved": False})
            if parsed.path == "/api/models/test":
                config = MODEL_SESSIONS.get(payload.get("session_id"))
                if not config: return json_response(self, {"error": "模型配置会话已失效，请重新保存"}, 400)
                if config.get("demo_mode"): return json_response(self, {"ok": True, "message": "演示模式连通性正常，未调用外部模型"})
                test_topic = {"title": "连接测试", "category": "测试", "audience": "测试用户", "difficulty": "基础"}
                data, _ = generate_content(test_topic, config); validate_map(data)
                return json_response(self, {"ok": True, "message": "模型返回有效 JSON 结构"})
            if parsed.path == "/api/tasks":
                topic_ids = [int(x) for x in payload.get("topic_ids", [])]
                if not topic_ids: return json_response(self, {"error": "请先添加主题"}, 400)
                batch_id = uuid.uuid4().hex[:12]
                with DB_LOCK, db() as conn:
                    conn.execute("INSERT INTO batches(id,total,model_name,created_at,updated_at) VALUES(?,?,?,?,?)", (batch_id, len(topic_ids), payload.get("model_name", "演示模式"), now(), now()))
                    conn.executemany("UPDATE topics SET batch_id=?,status=?,updated_at=? WHERE id=?", [(batch_id, "待生成", now(), topic_id) for topic_id in topic_ids])
                config = MODEL_SESSIONS.get(payload.get("session_id"), {"demo_mode": True})
                with TASK_LOCK: TASKS[batch_id] = {"cancelled": False}
                threading.Thread(target=run_batch, args=(batch_id, topic_ids, config), daemon=True).start()
                return json_response(self, {"batch_id": batch_id})
            if parsed.path == "/api/tasks/cancel":
                with TASK_LOCK:
                    if payload.get("batch_id") in TASKS: TASKS[payload["batch_id"]]["cancelled"] = True
                return json_response(self, {"ok": True})
            if parsed.path == "/api/export-zip":
                batch_id = payload.get("batch_id")
                source = OUTPUTS / batch_id
                if not source.is_dir(): return json_response(self, {"error": "批次输出不存在"}, 404)
                archive = unique_path(OUTPUTS, batch_id, ".zip")
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                    for file in source.rglob("*"):
                        if file.is_file(): zf.write(file, file.relative_to(source))
                return json_response(self, {"path": str(archive.relative_to(ROOT))})
            return json_response(self, {"error": "Not found"}, 404)
        except Exception as exc:
            return json_response(self, {"error": str(exc)}, 500)


if __name__ == "__main__":
    init_db()
    print("ProcessOn Digital Product Factory: http://127.0.0.1:4174")
    ThreadingHTTPServer(("127.0.0.1", 4174), Handler).serve_forever()
