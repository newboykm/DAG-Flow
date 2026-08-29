"""结构化输出的安全解析与校验（对齐业界 strict JSON 解析）。

模型返回 JSON 常见问题：加了一段前后缀文字、用 ```json 围栏、字段类型/缺失。
这里统一处理：剥围栏 → 定位首个 { 与末个 } → json.loads → 字段类型归一化 → 校验回调。
失败/不符时抛出，由调用方回退到默认值（避免静默降级丢失信息）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable


def strip_fences(text: str) -> str:
    """剥掉 ```json ... ``` 围栏与首尾空白。"""
    if not text:
        return ""
    s = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", s, re.S)
    if m:
        return m.group(1).strip()
    return s


def extract_json_block(text: str) -> str:
    """在文本中定位并截取最外层 JSON 对象 { ... }。找不到抛 ValueError。"""
    s = strip_fences(text)
    start = s.find("{")
    if start == -1:
        raise ValueError("no JSON object found")
    # 简单括号配对（足够处理模型输出）
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    raise ValueError("unbalanced JSON")


def parse_json_object(text: str) -> dict:
    """容错解析任意模型文本为 Python dict。失败抛 ValueError。"""
    block = extract_json_block(text)
    obj = json.loads(block)
    if not isinstance(obj, dict):
        raise ValueError(f"JSON is {type(obj).__name__}, expected dict")
    return obj


def expect_str(obj: dict, key: str, default: str = "") -> str:
    """取字符串字段；非字符串转字符串，缺失用默认。"""
    v = obj.get(key)
    if v is None:
        return default
    if isinstance(v, str):
        return v
    return str(v)


def expect_list(obj: dict, key: str, default=None) -> list:
    """取列表字段并过滤非标量项；缺失/非列表返回默认。"""
    v = obj.get(key)
    if v is None:
        return list(default or [])
    if not isinstance(v, list):
        return list(default or []) if not isinstance(v, (str, int, float)) else [v]
    return v


def expect_bool(obj: dict, key: str, default: bool = False) -> bool:
    v = obj.get(key)
    if isinstance(v, bool):
        return v
    if v in (0, 1):
        return bool(v)
    return default
