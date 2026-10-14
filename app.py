from __future__ import annotations

import ctypes
import csv
import io
import json
import os
import re
import shutil
import sqlite3
import sys
from ctypes import wintypes
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

from pos_export import verify_pos, write_pos

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
CRED_TARGET = "ProcessOnMindmapFactory/APIKey"

GRAPH_TYPES = {
    "思维导图": {
        "structure_class": "org.xmind.ui.map.clockwise",
        "layout": "中心主题按现代 XMind 平衡结构向两侧展开，按主题层级组织概念、方法和案例。",
        "rules": "围绕中心主题向左右两侧展开多个内容分支，一级分支尽量在左右均衡分布，强调概念之间的层级关系，不要强行生成先后步骤。",
    },
    "流程图": {
        "structure_class": "org.xmind.ui.logic.right",
        "layout": "从左到右表达开始、处理步骤、判断分支、回流和结束。",
        "rules": "必须按流程顺序组织；使用开始、步骤、判断条件、分支结果和结束等节点名称。判断节点的 children 表示不同条件路径。",
    },
    "鱼骨图": {
        "structure_class": "org.xmind.ui.fishbone.rightHeaded",
        "layout": "主问题作为鱼头，一级分支作为原因骨干，深层节点作为原因证据或改进措施。",
        "rules": "先明确一个结果/问题，再按原因类别展开；一级分支必须是原因类别，不要把内容写成普通知识目录。",
    },
    "树状结构图": {
        "structure_class": "org.xmind.ui.tree.right",
        "layout": "从根节点向右逐级展开，强调分类、从属和归纳关系。",
        "rules": "根节点下按分类逐级展开；同级节点应属于同一分类维度，不要混入时间顺序或流程判断。",
    },
    "双边思维导图": {
        "structure_class": "org.xmind.ui.map",
        "layout": "中心主题两侧平衡展开，左右分支共同承载内容。",
        "rules": "围绕中心主题形成左右两组主要分支；左侧和右侧都要有内容，适合对比、正反、输入输出或两类知识并列。",
    },
    "组织结构图": {
        "structure_class": "org.xmind.ui.org-chart.down",
        "layout": "从上到下表达总负责人、部门/模块和下属岗位或职责。",
        "rules": "按上级到下级组织节点；一级分支是部门、模块或角色，子节点是岗位、职责、成员或下属单元。",
    },
}


def graph_type_config(chart_type):
    return GRAPH_TYPES.get(str(chart_type or "思维导图").strip(), GRAPH_TYPES["思维导图"])


def now():
    return datetime.now().isoformat(timespec="seconds")


def db():
    DATA.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def credential_read():
    if os.name != "nt":
        return ""
    try:
        advapi = ctypes.WinDLL("Advapi32.dll")
        class CREDENTIAL(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)), ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]
        pointer = ctypes.POINTER(CREDENTIAL)()
        if not advapi.CredReadW(CRED_TARGET, 1, 0, ctypes.byref(pointer)):
            return ""
        try:
            raw = ctypes.string_at(pointer.contents.CredentialBlob, pointer.contents.CredentialBlobSize)
            return raw.decode("utf-16-le") if raw else ""
        finally:
            advapi.CredFree(pointer)
    except Exception:
        return ""


def credential_write(value):
    if os.name != "nt":
        raise RuntimeError("Windows 凭据存储仅在 Windows 上可用")
    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.c_void_p), ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]
    encoded = value.encode("utf-16-le")
    buffer = ctypes.create_string_buffer(encoded)
    item = CREDENTIAL(0, 1, CRED_TARGET, "ProcessOn 本地 API Key", wintypes.FILETIME(), len(encoded), ctypes.cast(buffer, ctypes.c_void_p), 2, 0, None, None, "ProcessOnMindmapFactory")
    if not ctypes.WinDLL("Advapi32.dll").CredWriteW(ctypes.byref(item), 0):
        raise ctypes.WinError()


def get_setting(key, default=""):
    with DB_LOCK, db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with DB_LOCK, db() as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def saved_model_config():
    config = {"base_url": get_setting("model.base_url") or "https://api.deepseek.com", "model": get_setting("model.id") or "deepseek-chat", "timeout": get_setting("model.timeout", "90"), "retries": get_setting("model.retries", "1"), "temperature": 0.4, "demo_mode": get_setting("model.demo", "1") == "1"}
    config["api_key"] = credential_read()
    if config["api_key"]:
        config["demo_mode"] = False
    return config


def init_db():
    with DB_LOCK, db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS topics (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL,
          category TEXT NOT NULL DEFAULT '未分类',
          audience TEXT NOT NULL DEFAULT '个人学习者',
          difficulty TEXT NOT NULL DEFAULT '标准',
          chart_type TEXT NOT NULL DEFAULT '思维导图',
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
        topic_columns = {row["name"] for row in conn.execute("PRAGMA table_info(topics)")}
        if "chart_type" not in topic_columns:
            conn.execute("ALTER TABLE topics ADD COLUMN chart_type TEXT NOT NULL DEFAULT '思维导图'")


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


def make_batch_id(title):
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slug(title)[:48]}-{uuid.uuid4().hex[:4]}"


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
    """Extract the first complete JSON object from a model response.

    Providers may wrap JSON in Markdown, add a short explanation, or include
    reasoning tags before the answer. A greedy first/last brace slice breaks
    when any of those wrappers contain braces, so scan candidate boundaries
    and let the JSON decoder identify the complete object.
    """
    if not isinstance(text, str):
        raise ValueError("模型返回内容不是文本")
    text = text.replace("\ufeff", "").strip()
    text = re.sub(r"<think(?:ing)?[^>]*>.*?</think(?:ing)?>", "", text, flags=re.I | re.S)
    decoder = json.JSONDecoder()
    candidates = []
    fenced = re.findall(r"```(?:json)?\s*(.*?)\s*```", text, re.I | re.S)
    candidates.extend(fenced)
    candidates.append(text)
    for candidate in candidates:
        candidate = candidate.strip()
        for index, char in enumerate(candidate):
            if char != "{":
                continue
            try:
                _, end = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            return candidate[index:index + end]
    raise ValueError("模型返回不是合法 JSON，请重试")


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
        if children:
            child_count, child_depth, child_names = walk_nodes(children, depth + 1)
            count += child_count
            max_depth = max(max_depth, child_depth)
            names.extend(child_names)
    return count, max_depth, names


DIFFICULTY_LIMITS = {
    "基础": {"min_nodes": 20, "max_nodes": 80, "max_depth": 3},
    "标准": {"min_nodes": 30, "max_nodes": 140, "max_depth": 4},
    "进阶": {"min_nodes": 60, "max_nodes": 240, "max_depth": 4},
}


def difficulty_limits(difficulty):
    return DIFFICULTY_LIMITS.get(str(difficulty or "标准").strip(), DIFFICULTY_LIMITS["标准"])


def trim_nodes_to_limit(branches, max_nodes):
    """Keep the hierarchy and remove deepest trailing nodes until within budget."""
    kept = 0

    def trim_level(nodes):
        nonlocal kept
        result = []
        for node in nodes or []:
            if kept >= max_nodes:
                break
            copy = {key: value for key, value in node.items() if key != "children"}
            copy["name"] = str(node.get("name", "")).strip()
            copy["children"] = []
            result.append(copy)
            kept += 1
        return result

    roots = trim_level(branches)
    source_to_copy = list(zip(branches[:len(roots)], roots))
    while source_to_copy and kept < max_nodes:
        next_level = []
        child_index = 0
        while kept < max_nodes:
            added = False
            for source, target in source_to_copy:
                children = source.get("children", [])
                if child_index >= len(children) or kept >= max_nodes:
                    continue
                child = children[child_index]
                copy = {key: value for key, value in child.items() if key != "children"}
                copy["name"] = str(child.get("name", "")).strip()
                copy["children"] = []
                target["children"].append(copy)
                next_level.append((child, copy))
                kept += 1
                added = True
            if not added:
                break
            child_index += 1
        source_to_copy = next_level
    return roots


def collapse_nodes_to_depth(branches, max_depth):
    """Flatten paths deeper than max_depth into readable leaf nodes."""
    collapsed = 0

    def leaf_paths(node, prefix=None):
        path = (prefix or []) + [str(node.get("name", "")).strip()]
        children = node.get("children", [])
        if not children:
            return [path]
        paths = []
        for child in children:
            paths.extend(leaf_paths(child, path))
        return paths

    def process(nodes, depth):
        nonlocal collapsed
        result = []
        for node in nodes or []:
            copy = {key: value for key, value in node.items() if key != "children"}
            copy["name"] = str(node.get("name", "")).strip()
            children = node.get("children", [])
            if depth < max_depth:
                copy["children"] = process(children, depth + 1)
                result.append(copy)
                continue
            if not children:
                copy["children"] = []
                result.append(copy)
                continue
            paths = leaf_paths(node)
            collapsed += sum(len(path) - 1 for path in paths)
            for path in paths:
                name = " / ".join(path)
                result.append({"name": name[:80], "children": []})
        return result

    return process(branches, 1), collapsed


def normalize_map_budget(data, difficulty=None):
    requested_difficulty = difficulty or data.get("difficulty") or "标准"
    limits = difficulty_limits(requested_difficulty)
    count, depth, _ = walk_nodes(data["branches"])
    notes = data.setdefault("quality_notes", [])
    if depth > limits["max_depth"]:
        data["branches"], collapsed = collapse_nodes_to_depth(data["branches"], limits["max_depth"])
        notes.append(f"模型原始层级为 {depth} 层，已按{requested_difficulty}难度上限自动压平为 {limits['max_depth']} 层。")
        data["_collapsed_levels"] = collapsed
    count, _, _ = walk_nodes(data["branches"])
    if count > limits["max_nodes"]:
        data["branches"] = trim_nodes_to_limit(data["branches"], limits["max_nodes"])
        removed = count - limits["max_nodes"]
        notes.append(f"模型原始输出 {count} 个节点，已按{requested_difficulty}难度上限自动裁剪 {removed} 个末端节点。")
        data["_trimmed_nodes"] = removed
    return data


def validate_map(data, difficulty=None, enforce_budget=True):
    required = ["title", "category", "audience", "difficulty", "branches"]
    missing = [key for key in required if not data.get(key)]
    if missing:
        raise ValueError("缺少字段：" + ", ".join(missing))
    if not isinstance(data["branches"], list) or not data["branches"]:
        raise ValueError("branches 必须包含至少一个一级分支")
    requested_difficulty = difficulty or data.get("difficulty") or "标准"
    limits = difficulty_limits(requested_difficulty)
    count, depth, names = walk_nodes(data["branches"])
    if enforce_budget and count > limits["max_nodes"]:
        raise ValueError(f"{requested_difficulty}主题节点数量 {count} 超过 {limits['max_nodes']} 个上限")
    if enforce_budget and depth > limits["max_depth"]:
        raise ValueError(f"{requested_difficulty}主题层级 {depth} 超过 {limits['max_depth']} 层上限")
    duplicates = sorted({name for name in names if names.count(name) > 1})
    data["_node_count"] = count
    data["_max_depth"] = depth
    data["_duplicates"] = duplicates[:20]
    return data


def demo_map(topic):
    """Return an offline fixture whose top-level shape matches the selected chart type."""
    category = topic["category"]
    title = topic["title"]
    chart_type = topic.get("chart_type", "思维导图")
    if chart_type == "流程图":
        stems = [("开始", ["明确输入与目标"]), ("准备资料", ["收集信息", "检查信息是否完整"]), ("判断条件", ["满足条件：进入处理", "不满足条件：补充资料"]), ("执行处理", ["完成核心动作", "记录处理结果"]), ("输出结果", ["交付结果", "进入复盘"]), ("结束", ["流程完成"])]
    elif chart_type == "鱼骨图":
        stems = [("人员", ["角色能力", "操作责任"]), ("方法", ["流程规范", "执行步骤"]), ("材料", ["输入资料", "资源质量"]), ("设备", ["工具配置", "系统稳定性"]), ("环境", ["外部条件", "时间约束"]), ("测量", ["指标口径", "检查方式"])]
    elif chart_type == "双边思维导图":
        stems = [("左侧：原因与输入", ["背景条件", "关键问题", "已有资源"]), ("右侧：方案与输出", ["解决路径", "应用结果", "风险边界"])]
    elif chart_type == "组织结构图":
        stems = [("管理层", ["目标与决策", "资源协调"]), ("业务模块", ["核心工作", "交付结果"]), ("支持模块", ["工具支持", "质量与合规"])]
    elif chart_type == "树状结构图":
        stems = [("核心分类 A", ["子类 A1", "子类 A2", "子类 A3"]), ("核心分类 B", ["子类 B1", "子类 B2", "子类 B3"]), ("核心分类 C", ["子类 C1", "子类 C2", "子类 C3"])]
    else:
        marketing_topic = any(word in title for word in ("计量营销", "营销计量", "营销分析"))
        stems = [
            ("研究对象与变量", ["消费者行为与市场结果", "处理变量、结果变量与控制变量", "相关关系与因果关系"]),
            ("营销数据与测量" if marketing_topic else "理论框架与机制", ["问卷量表与信度效度" if marketing_topic else "核心理论假设", "交易、广告与客户行为数据" if marketing_topic else "作用机制与影响路径", "转化率、留存率与客户终身价值" if marketing_topic else "适用条件与边界"]),
            ("回归与因果推断" if marketing_topic else "数据与指标体系", ["线性回归与对数变换" if marketing_topic else "数据来源与数据结构", "混杂因素与遗漏变量偏差" if marketing_topic else "关键指标与计算口径", "工具变量、双重差分与随机实验" if marketing_topic else "测量误差与数据质量"]),
            ("实验与识别策略" if marketing_topic else "模型与分析方法", ["A/B 测试与随机分组" if marketing_topic else "模型结构与参数", "处理效应与异质性效应" if marketing_topic else "方法选择与适用条件", "样本量、统计功效与外部效度" if marketing_topic else "估计步骤与识别要求"]),
            ("需求与选择模型" if marketing_topic else "结果解释与诊断", ["价格弹性与需求估计" if marketing_topic else "结果读取与实际含义", "离散选择模型与效用函数" if marketing_topic else "模型诊断与稳健性", "联合分析与属性重要性" if marketing_topic else "常见误读与局限"]),
            ("结果解释与应用" if marketing_topic else "典型应用与案例", ["系数、边际效应与置信区间" if marketing_topic else "典型业务问题", "模型诊断与稳健性检验" if marketing_topic else "方法落地步骤", "预算分配、定价与广告归因" if marketing_topic else "结论使用边界"]),
        ]
    branches = [{"name": name, "children": [{"name": item, "children": [{"name": "关键定义、执行条件或适用边界", "children": []}]} for item in items]} for name, items in stems]
    return {"title": title, "category": category, "audience": topic["audience"], "difficulty": topic["difficulty"], "chart_type": chart_type,
            "branches": branches, "quality_notes": ["当前为离线结构样例，不是该主题的完整专业内容；正式生成请关闭演示模式并调用真实模型"],
            "sources_to_verify": ["请根据教材、论文、官方统计口径或课程大纲核验专业定义、公式和研究结论"]}


def model_prompt(topic):
    system = PROMPT_PATH.read_text(encoding="utf-8") if PROMPT_PATH.exists() else "只输出合法 JSON。"
    chart_type = topic.get("chart_type", "思维导图")
    chart = graph_type_config(chart_type)
    limits = difficulty_limits(topic.get("difficulty"))
    budget = (
        f"\n\n本次生成预算（必须遵守）：当前难度为{topic.get('difficulty', '标准')}，"
        f"节点数量控制在 {limits['min_nodes']}-{limits['max_nodes']} 个，最多 {limits['max_depth']} 层。"
        f"为避免统计口径误差，请把目标控制在不超过 {max(limits['min_nodes'], limits['max_nodes'] - 10)} 个节点，绝对不得超过 {limits['max_nodes']} 个。"
        "请优先保留真正重要的专业知识，不要重复、凑数或为了压缩内容删除关键关系。"
        f"\n\n本次图形类型：{chart_type}。目标布局：{chart['layout']}"
        f"\n图形结构硬性规则：{chart['rules']}"
        "\n输出必须能被该图形类型直接导入和阅读，不能用思维导图的通用目录替代所选结构。"
    )
    return system + budget + "\n\n本次主题 JSON：" + json.dumps(topic, ensure_ascii=False)


def call_model(topic, config):
    body = {
        "model": config["model"],
        "messages": [{"role": "user", "content": model_prompt(topic)}],
        "temperature": float(config.get("temperature", 0.4)),
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
    chart_type = data.get("chart_type", "思维导图")
    keywords = [data["category"], data["audience"], title, chart_type, "知识结构"]
    return {"suggested_title": title + f"｜{chart_type}结构图", "alternative_titles": [title + f"核心{chart_type}", title + f"{chart_type}应用框架"],
            "description": f"面向{data['audience']}的{data['category']}主题{chart_type}，按照所选图形的结构和布局组织内容。内容需结合个人学习目标进行核验和补充。",
            "audience": data["audience"], "scenarios": ["课程复习", "快速建立知识框架", "考前查漏补缺"],
            "highlights": [f"包含 {quality['node_count']} 个有效节点", f"最高 {quality['max_depth']} 层结构", "生成后可继续人工修改"],
            "chart_type": chart_type, "keywords": list(dict.fromkeys(keywords)), "category_suggestion": data["category"],
            "price_reference": "待人工定价：需结合同类商品、内容深度和平台表现判断，不代表实际收益。",
            "checklist": ["核验事实和参考资料", "检查导入后的布局", "删除个人隐私和课程内部资料", "确认标题和简介没有夸大承诺"]}


def write_xmind(folder, title, branches, chart_type="思维导图"):
    """Write an XMind 8 XML package that ProcessOn can import reliably."""
    content_ns = "urn:xmind:xmap:xmlns:content:2.0"
    style_ns = "urn:xmind:xmap:xmlns:style:2.0"
    meta_ns = "urn:xmind:xmap:xmlns:meta:2.0"
    manifest_ns = "urn:xmind:xmap:xmlns:manifest:1.0"
    ET.register_namespace("", content_ns)
    ET.register_namespace("", style_ns)
    ET.register_namespace("", meta_ns)
    ET.register_namespace("", manifest_ns)

    def add_topic(parent, name, children, node_id, depth=1):
        attributes = {"id": node_id}
        if children and depth >= 2:
            attributes["collapsed"] = "true"
        topic = ET.SubElement(parent, f"{{{content_ns}}}topic", attributes)
        ET.SubElement(topic, f"{{{content_ns}}}title").text = str(name)
        if children:
            children_el = ET.SubElement(topic, f"{{{content_ns}}}children")
            attached = ET.SubElement(children_el, f"{{{content_ns}}}topics", type="attached")
            for index, child in enumerate(children):
                add_topic(attached, child["name"], child.get("children", []), f"topic-{node_id}-{index}", depth + 1)
        return topic

    chart = graph_type_config(chart_type)
    content = ET.Element(f"{{{content_ns}}}xmap-content", version="2.0")
    sheet = ET.SubElement(content, f"{{{content_ns}}}sheet", id="sheet-1")
    ET.SubElement(sheet, f"{{{content_ns}}}title").text = title
    root_topic = ET.SubElement(sheet, f"{{{content_ns}}}topic", {"id": "root-topic", "structure-class": chart["structure_class"]})
    ET.SubElement(root_topic, f"{{{content_ns}}}title").text = title
    children_el = ET.SubElement(root_topic, f"{{{content_ns}}}children")
    attached = ET.SubElement(children_el, f"{{{content_ns}}}topics", type="attached")
    for index, branch in enumerate(branches):
        add_topic(attached, branch["name"], branch.get("children", []), f"topic-root-{index}", 1)

    styles = ET.Element(f"{{{style_ns}}}xmap-styles", version="2.0")
    ET.SubElement(styles, f"{{{style_ns}}}styles")
    meta = ET.Element(f"{{{meta_ns}}}meta", version="2.0")
    manifest = ET.Element(f"{{{manifest_ns}}}manifest")
    for path, media_type in (("content.xml", "text/xml"), ("styles.xml", "text/xml"), ("meta.xml", "text/xml")):
        ET.SubElement(manifest, f"{{{manifest_ns}}}file-entry", {"full-path": path, "media-type": media_type})

    xmind_path = unique_path(folder, title, ".xmind")
    with zipfile.ZipFile(xmind_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, element in (("content.xml", content), ("styles.xml", styles), ("meta.xml", meta), ("META-INF/manifest.xml", manifest)):
            buffer = io.BytesIO()
            ET.ElementTree(element).write(buffer, encoding="utf-8", xml_declaration=True)
            archive.writestr(name, buffer.getvalue())
    return xmind_path


def write_outputs(topic, data, quality, batch_id):
    folder = OUTPUTS / batch_id / slug(topic["title"])
    folder.mkdir(parents=True, exist_ok=True)
    data = {key: value for key, value in data.items() if not key.startswith("_")}
    raw_path = folder / "content.json"
    raw_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    title = data["title"]
    chart_type = data.get("chart_type", topic.get("chart_type", "思维导图"))
    xmind_path = write_xmind(folder, title, data["branches"], chart_type) if chart_type == "思维导图" else None
    pos_path = write_pos(folder, title, data["branches"], chart_type)
    if not verify_pos(pos_path):
        raise ValueError("ProcessOn POS 文件结构验证失败")
    quality_path = folder / "quality-report.json"
    quality_path.write_text(json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")
    materials = publish_materials(data, quality)
    (folder / "publish-materials.json").write_text(json.dumps(materials, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(folder.relative_to(ROOT)), str(pos_path.relative_to(ROOT)), str(xmind_path.relative_to(ROOT)) if xmind_path else ""


def insert_topics(items, batch_id=None):
    created = []
    with DB_LOCK, db() as conn:
        for item in items:
            title = str(item.get("title", "")).strip()
            if not title: continue
            chart_type = item.get("chart_type", "思维导图")
            if chart_type not in GRAPH_TYPES: chart_type = "思维导图"
            created.append(conn.execute("INSERT INTO topics(title,category,audience,difficulty,chart_type,batch_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (title, item.get("category", "未分类"), item.get("audience", "个人学习者"), item.get("difficulty", "标准"), chart_type, batch_id, now(), now())).lastrowid)
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
            topic["chart_type"] = topic.get("chart_type") if topic.get("chart_type") in GRAPH_TYPES else "思维导图"
            data, model_name = generate_content(topic, config)
            data["chart_type"] = topic["chart_type"]
            data = normalize_map_budget(data, topic.get("difficulty"))
            data = validate_map(data, topic.get("difficulty"))
            quality = quality_report(data)
            paths = write_outputs(topic, data, quality, batch_id)
            with DB_LOCK, db() as conn:
                conn.execute("UPDATE topics SET status=?,result_path=?,quality_score=?,error=?,updated_at=? WHERE id=?", ("待审核", paths[1], quality["score"], "", now(), topic_id))
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
        if parsed.path == "/api/models/config":
            saved = saved_model_config()
            return json_response(self, {"base_url": saved["base_url"], "model": saved["model"], "timeout": saved["timeout"], "retries": saved["retries"], "demo_mode": saved["demo_mode"], "key_saved": bool(saved["api_key"])})
        if parsed.path == "/api/state":
            with DB_LOCK, db() as conn:
                batches = [dict(row) for row in conn.execute("SELECT * FROM batches ORDER BY created_at DESC LIMIT 20")]
            return json_response(self, {"topics": list_topics(), "batches": batches, "output_dir": str(OUTPUTS)})
        if parsed.path == "/api/download":
            rel = parse_qs(parsed.query).get("path", [""])[0]
            candidate = (ROOT / rel).resolve()
            if ROOT not in candidate.parents or not candidate.is_file(): return json_response(self, {"error": "文件不存在"}, 404)
            return self.send_file(candidate, "application/octet-stream")
        if parsed.path == "/api/open-location":
            rel = parse_qs(parsed.query).get("path", [""])[0]
            candidate = (ROOT / rel).resolve()
            if ROOT not in candidate.parents or not candidate.exists(): return json_response(self, {"error": "存储位置不存在"}, 404)
            target = candidate if candidate.is_dir() else candidate.parent
            if hasattr(os, "startfile"):
                os.startfile(str(target))
            return json_response(self, {"ok": True, "path": str(target)})

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
            if parsed.path == "/api/models/config":
                saved = saved_model_config()
                return json_response(self, {"base_url": saved["base_url"], "model": saved["model"], "timeout": saved["timeout"], "retries": saved["retries"], "demo_mode": saved["demo_mode"], "key_saved": bool(saved["api_key"])})
            if parsed.path == "/api/models/session":
                session_id = uuid.uuid4().hex
                incoming = payload.get("config", {})
                config = saved_model_config()
                config.update({key: incoming.get(key, config.get(key)) for key in ("base_url", "model", "timeout", "retries", "demo_mode", "temperature")})
                if incoming.get("api_key"):
                    credential_write(incoming["api_key"])
                    config["api_key"] = incoming["api_key"]
                for key, setting_key in (("base_url", "model"), ("timeout", "retries")):
                    set_setting("model." + ("id" if key == "model" else key), str(config.get(key, "")))
                set_setting("model.demo", "1" if config.get("demo_mode") else "0")
                if not config.get("demo_mode") and (not config.get("base_url") or not config.get("model") or not config.get("api_key")):
                    return json_response(self, {"error": "请填写 API 地址、模型 ID 和 API Key；Key 会保存到 Windows 凭据管理器"}, 400)
                MODEL_SESSIONS[session_id] = config
                return json_response(self, {"session_id": session_id, "key_saved": bool(config.get("api_key"))})
            if parsed.path == "/api/models/test":
                config = MODEL_SESSIONS.get(payload.get("session_id"))
                if not config: return json_response(self, {"error": "模型配置会话已失效，请重新保存"}, 400)
                if config.get("demo_mode"): return json_response(self, {"ok": True, "message": "演示模式连通性正常，未调用外部模型"})
                test_topic = {"title": "连接测试", "category": "测试", "audience": "测试用户", "difficulty": "标准"}
                data, _ = generate_content(test_topic, config); validate_map(data, "标准", enforce_budget=False)
                return json_response(self, {"ok": True, "message": "模型返回有效 JSON 结构"})
            if parsed.path == "/api/tasks":
                topic_ids = [int(x) for x in payload.get("topic_ids", [])]
                if not topic_ids: return json_response(self, {"error": "请先添加主题"}, 400)
                difficulty = payload.get("difficulty", "标准")
                if difficulty not in DIFFICULTY_LIMITS: difficulty = "标准"
                chart_type = payload.get("chart_type")
                with DB_LOCK, db() as conn:
                    first_topic = conn.execute("SELECT title,chart_type FROM topics WHERE id=?", (topic_ids[0],)).fetchone()
                    first_title = first_topic["title"] if first_topic else "图形批次"
                    chart_type = chart_type or (first_topic["chart_type"] if first_topic else "思维导图")
                if chart_type not in GRAPH_TYPES: chart_type = "思维导图"
                batch_id = make_batch_id(first_title)
                with DB_LOCK, db() as conn:
                    conn.execute("INSERT INTO batches(id,total,model_name,created_at,updated_at) VALUES(?,?,?,?,?)", (batch_id, len(topic_ids), payload.get("model_name", "演示模式"), now(), now()))
                    conn.executemany("UPDATE topics SET batch_id=?,difficulty=?,chart_type=?,status=?,error=?,updated_at=? WHERE id=?", [(batch_id, difficulty, chart_type, "待生成", "", now(), topic_id) for topic_id in topic_ids])
                config = MODEL_SESSIONS.get(payload.get("session_id"), saved_model_config())
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
