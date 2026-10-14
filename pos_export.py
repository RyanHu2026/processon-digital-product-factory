from __future__ import annotations

import json
import math
import random
import re
import string
import uuid
from datetime import datetime
from pathlib import Path


ANCHOR_ANGLES = {
    0: math.pi / 2,
    1: math.pi * 1.5,
    2: 0,
    3: math.pi,
}
ANCHORS = [
    {"x": "w/2", "y": "0"},
    {"x": "w/2", "y": "h"},
    {"x": "0", "y": "h/2"},
    {"x": "w", "y": "h/2"},
]
ATTR = {
    "container": False,
    "rotatable": True,
    "visible": True,
    "fixedLink": False,
    "collapsable": False,
    "collapsed": False,
    "linkable": True,
    "markerOffset": 5,
}
RESIZE = ["tl", "tr", "br", "bl", "l", "t", "r", "b"]

PATH_PROCESS = [{"actions": [
    {"x": "0", "action": "move", "y": "0"},
    {"x": "w", "action": "line", "y": "0"},
    {"x": "w", "action": "line", "y": "h"},
    {"x": "0", "action": "line", "y": "h"},
    {"action": "close"},
]}]
PATH_DECISION = [{"actions": [
    {"x": "0", "action": "move", "y": "h/2"},
    {"x": "w/2", "action": "line", "y": "0"},
    {"x": "w", "action": "line", "y": "h/2"},
    {"x": "w/2", "action": "line", "y": "h"},
    {"x": "0", "action": "line", "y": "h/2"},
    {"action": "close"},
]}]
PATH_TERMINATOR = [{"actions": [
    {"x": "Math.min(w,h)/3", "action": "move", "y": "0"},
    {"x": "w-Math.min(w,h)/3", "action": "line", "y": "0"},
    {"y1": "0", "x": "w-Math.min(w,h)/3", "action": "curve", "x1": "w+Math.min(w,h)/9", "y2": "h", "y": "h", "x2": "w+Math.min(w,h)/9"},
    {"x": "Math.min(w,h)/3", "action": "line", "y": "h"},
    {"y1": "h", "x": "Math.min(w,h)/3", "action": "curve", "x1": "-Math.min(w,h)/9", "y2": "0", "y": "0", "x2": "-Math.min(w,h)/9"},
    {"action": "close"},
]}]
PATH_ROUND = [{"actions": [
    {"x": "0", "action": "move", "y": "4"},
    {"y1": "0", "x": "4", "action": "quadraticCurve", "y": "0", "x1": "0"},
    {"x": "w-4", "action": "line", "y": "0"},
    {"y1": "0", "x": "w", "action": "quadraticCurve", "y": "4", "x1": "w"},
    {"x": "w", "action": "line", "y": "h-4"},
    {"y1": "h", "x": "w-4", "action": "quadraticCurve", "y": "h", "x1": "w"},
    {"x": "4", "action": "line", "y": "h"},
    {"y1": "h", "x": "0", "action": "quadraticCurve", "y": "h-4", "x1": "0"},
    {"action": "close"},
]}]


def gen_id():
    letters = "".join(random.choice(string.ascii_letters) for _ in range(10))
    digits = "".join(random.choice(string.digits) for _ in range(6))
    return letters + digits


class PosBuilder:
    def __init__(self):
        self.elements = {}
        self._zindex = 0

    def _next_z(self):
        self._zindex += 1
        return self._zindex

    def add_shape(self, name, title, text, x, y, w=190, h=58, path=None, fill="", font="0,0,0"):
        node_id = gen_id()
        self.elements[node_id] = {
            "link": "",
            "shapeStyle": {"alpha": 1},
            "textBlock": [{"position": {"w": "w-20", "x": 10, "h": "h", "y": 0}, "text": text}],
            "anchors": ANCHORS,
            "title": title,
            "fontStyle": {"size": 15, "color": font, "textAlign": "center", "vAlign": "middle"},
            "dataAttributes": [],
            "props": {"zindex": self._next_z(), "w": w, "x": x, "h": h, "y": y, "angle": 0},
            "path": path or PATH_PROCESS,
            "lineStyle": {"lineWidth": 1.5, "lineColor": "93,105,120"},
            "children": [],
            "resizeDir": RESIZE,
            "name": name,
            "fillStyle": {"color": fill, "type": "solid"} if fill else {},
            "theme": {},
            "id": node_id,
            "attribute": dict(ATTR),
            "category": "flow" if name in {"process", "decision", "terminator", "start"} else "basic",
            "locked": False,
            "group": "",
        }
        return node_id

    def process(self, text, x, y, **kwargs):
        return self.add_shape("process", "流程", text, x, y, path=PATH_PROCESS, **kwargs)

    def decision(self, text, x, y, **kwargs):
        return self.add_shape("decision", "判断", text, x, y, w=170, h=82, path=PATH_DECISION, **kwargs)

    def terminator(self, text, x, y, **kwargs):
        return self.add_shape("terminator", "开始/结束", text, x, y, w=150, h=52, path=PATH_TERMINATOR, **kwargs)

    def round_box(self, text, x, y, **kwargs):
        return self.add_shape("roundRectangle", "圆角矩形", text, x, y, path=PATH_ROUND, **kwargs)

    def add_linker(self, source_id, target_id, label="", source_anchor=1, target_anchor=0):
        source = self.elements[source_id]["props"]
        target = self.elements[target_id]["props"]

        def anchor(props, index):
            x, y, w, h = props["x"], props["y"], props["w"], props["h"]
            return ((x + w / 2, y), (x + w / 2, y + h), (x, y + h / 2), (x + w, y + h / 2))[index]

        sx, sy = anchor(source, source_anchor)
        tx, ty = anchor(target, target_anchor)
        source_horizontal = source_anchor in (2, 3)
        target_horizontal = target_anchor in (2, 3)
        if source_horizontal and target_horizontal:
            middle = (sx + tx) / 2
            points = [{"x": middle, "y": sy}, {"x": middle, "y": ty}]
        elif not source_horizontal and not target_horizontal:
            middle = (sy + ty) / 2
            points = [{"x": sx, "y": middle}, {"x": tx, "y": middle}]
        elif source_horizontal:
            points = [{"x": tx, "y": sy}]
        else:
            points = [{"x": sx, "y": ty}]
        linker_id = gen_id()
        self.elements[linker_id] = {
            "textBlock": [{"location": 0.5, "text": label}] if label else [],
            "fontStyle": {"size": 14, "bold": True} if label else {},
            "points": points,
            "dataAttributes": [],
            "props": {"zindex": self._next_z()},
            "linkerType": "broken",
            "lineStyle": {"lineWidth": 1.5, "beginArrowStyle": "none", "endArrowStyle": "solidArrow"},
            "lineType": "solid",
            "name": "linker",
            "from": {"x": sx, "y": sy, "angle": ANCHOR_ANGLES[source_anchor], "id": source_id},
            "id": linker_id,
            "text": label,
            "to": {"x": tx, "y": ty, "angle": ANCHOR_ANGLES[target_anchor], "id": target_id},
            "locked": False,
            "group": "",
        }
        return linker_id

    def build(self, title):
        max_x = max((item.get("props", {}).get("x", 0) + item.get("props", {}).get("w", 0) for item in self.elements.values()), default=1200)
        max_y = max((item.get("props", {}).get("y", 0) + item.get("props", {}).get("h", 0) for item in self.elements.values()), default=800)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return {
            "diagram": {"elements": {
                "editorVersion": "V1",
                "comments": [],
                "plugins": {},
                "elements": self.elements,
                "page": {"padding": 20, "backgroundColor": "transparent", "orientation": "landscape", "gridSize": 15, "width": int(max_x + 100), "showGrid": True, "lineJumps": False, "height": int(max_y + 100)},
                "version": 0,
                "aiContainers": {},
            }},
            "meta": {
                "exportTime": timestamp,
                "member": "",
                "diagramInfo": {"creator": "ProcessOn Product Factory", "created": timestamp, "modified": timestamp, "title": title, "category": "flow"},
                "id": uuid.uuid4().hex[:24],
                "type": "ProcessOn Schema File",
                "version": "5.0",
            },
        }


def _leaf_count(node):
    children = node.get("children", [])
    return sum(_leaf_count(child) for child in children) if children else 1


def _tree_layout(builder, title, branches, top_down=True, organization=False):
    total_leaves = max(1, sum(_leaf_count(branch) for branch in branches))
    root_x = max(300, total_leaves * 75)
    root = builder.round_box(title, root_x, 60, w=220, h=64, fill="45,129,247", font="255,255,255")
    cursor = 120

    def place(node, depth, parent_id):
        nonlocal cursor
        children = node.get("children", [])
        if children:
            child_positions = [place(child, depth + 1, None) for child in children]
            center = sum(position[1] for position in child_positions) / len(child_positions)
        else:
            center = cursor
            cursor += 95
        x = root_x + (depth - 1) * 270 if not top_down else center
        y = center if not top_down else 60 + depth * 150
        node_id = builder.round_box(node["name"], x, y, w=205, h=58, fill="232,240,254" if not organization else "235,246,239")
        for child_id, _ in child_positions if children else []:
            if top_down:
                builder.add_linker(node_id, child_id, source_anchor=1, target_anchor=0)
            else:
                builder.add_linker(node_id, child_id, source_anchor=3, target_anchor=2)
        if parent_id:
            if top_down:
                builder.add_linker(parent_id, node_id, source_anchor=1, target_anchor=0)
            else:
                builder.add_linker(parent_id, node_id, source_anchor=3, target_anchor=2)
        return node_id, center

    for branch in branches:
        branch_id, _ = place(branch, 1, None)
        builder.add_linker(root, branch_id, source_anchor=1 if top_down else 3, target_anchor=0 if top_down else 2)


def _mindmap_layout(builder, title, branches):
    total = max(1, len(branches))
    root_y = max(400, total * 100)
    root_x = 1050
    root = builder.round_box(title, root_x, root_y, w=230, h=68, fill="40,167,130", font="255,255,255")
    side_cursors = {"left": 100, "right": 100}

    def place(node, side, depth, parent_id):
        children = node.get("children", [])
        if children:
            child_ids = [place(child, side, depth + 1, None) for child in children]
            center = sum(item[1] for item in child_ids) / len(child_ids)
        else:
            center = side_cursors[side]
            side_cursors[side] += 88
        x = root_x - depth * 280 if side == "left" else root_x + depth * 280
        node_id = builder.round_box(node["name"], x, center, w=205, h=54, fill="239,244,250")
        for child_id, _ in child_ids if children else []:
            builder.add_linker(node_id, child_id, source_anchor=2 if side == "left" else 3, target_anchor=3 if side == "left" else 2)
        if parent_id:
            builder.add_linker(parent_id, node_id, source_anchor=2 if side == "left" else 3, target_anchor=3 if side == "left" else 2)
        return node_id, center

    for index, branch in enumerate(branches):
        side = "left" if index % 2 == 0 else "right"
        branch_id, _ = place(branch, side, 1, None)
        builder.add_linker(root, branch_id, source_anchor=2 if side == "left" else 3, target_anchor=3 if side == "left" else 2)


def _flow_layout(builder, title, branches):
    start = builder.terminator("开始", 500, 50, fill="45,129,247", font="255,255,255")
    previous = start
    flattened = []

    def flatten(items):
        for item in items:
            flattened.append(item["name"])
            flatten(item.get("children", []))

    flatten(branches)
    for index, text in enumerate(flattened):
        y = 145 + index * 110
        if any(word in text for word in ("是否", "判断", "条件", "检查", "确认")):
            current = builder.decision(text, 490, y, fill="255,243,205")
        else:
            current = builder.process(text, 480, y, w=210, fill="232,240,254")
        builder.add_linker(previous, current)
        previous = current
    end = builder.terminator("结束", 510, 160 + len(flattened) * 110, fill="40,167,130", font="255,255,255")
    builder.add_linker(previous, end)


def _fishbone_layout(builder, title, branches):
    effect = builder.round_box(title, 1250, 430, w=240, h=70, fill="45,129,247", font="255,255,255")
    spine = builder.round_box("原因主线", 250, 440, w=900, h=16, fill="93,105,120")
    builder.add_linker(spine, effect, source_anchor=3, target_anchor=2)
    for index, branch in enumerate(branches):
        above = index % 2 == 0
        x = 300 + (index // 2) * 250
        y = 190 if above else 650
        cause = builder.round_box(branch["name"], x, y, w=190, h=55, fill="255,243,205")
        builder.add_linker(cause, spine, source_anchor=1 if above else 0, target_anchor=0 if above else 1)
        for child_index, child in enumerate(branch.get("children", [])[:4]):
            child_y = y - 85 - child_index * 62 if above else y + 90 + child_index * 62
            detail = builder.round_box(child["name"], x - 40, child_y, w=190, h=48, fill="247,249,252")
            builder.add_linker(detail, cause, source_anchor=1 if above else 0, target_anchor=0 if above else 1)


def write_pos(folder: Path, title: str, branches: list, chart_type: str):
    builder = PosBuilder()
    if chart_type == "流程图":
        _flow_layout(builder, title, branches)
    elif chart_type == "鱼骨图":
        _fishbone_layout(builder, title, branches)
    elif chart_type == "树状结构图":
        _tree_layout(builder, title, branches, top_down=False)
    elif chart_type == "组织结构图":
        _tree_layout(builder, title, branches, top_down=True, organization=True)
    else:
        _mindmap_layout(builder, title, branches)
    data = builder.build(title)
    safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(title)).strip(" .") or "diagram"
    path = folder / f"{safe_title}.pos"
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return path


def verify_pos(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    elements = data["diagram"]["elements"]["elements"]
    for element in elements.values():
        if element.get("name") == "linker":
            if element["from"]["id"] not in elements or element["to"]["id"] not in elements:
                return False
    return data["meta"]["type"] == "ProcessOn Schema File" and bool(elements)
