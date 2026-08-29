"""项目上下文加载（对齐业界 agent 的「开工先读项目」）。

在 agent 开工前扫描工作区，生成一份紧凑的项目概览注入 system prompt，
让模型在动手前就了解项目结构与约定（而不只是被动等用户问）。

- 读取：README/AGENTS.md/AGENTS/等约定文件 + 顶层目录树。
- 结果按 workspace 缓存（带时间戳，避免每次调用都重扫）。
"""
from __future__ import annotations

import os
import time

_last_cache: dict[str, dict] = {}  # workspace -> {"ts": float, "text": str}
_CACHE_TTL = 600  # 10 分钟

# 优先读的约定/说明文件（按优先级）
_AGENTS_FILES = ["AGENTS.md", "README.md", "README.txt", "Readme.md"]
_CONVENTION_FILES = [".cursorrules", ".claude", "docs/"]


def _read_first(path: str, limit: int = 4000) -> str:
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read(limit)
        except Exception:
            return ""
    return ""


def _tree(workspace: str, max_depth: int = 2, max_items: int = 60) -> list[str]:
    """列目录树（跳过隐藏/常见噪音目录），限深度与数量。"""
    skip = {".git", ".agents", "node_modules", "__pycache__", ".venv", "venv", "dist", ".next", "target", ".reasonix"}
    out: list[str] = []

    def walk(d: str, depth: int):
        if depth > max_depth or len(out) >= max_items:
            return
        try:
            entries = sorted(os.listdir(d))
        except Exception:
            return
        for name in entries:
            if name in skip or name.startswith("."):
                continue
            full = os.path.join(d, name)
            indent = "  " * depth
            if os.path.isdir(full):
                out.append(f"{indent}{name}/")
                walk(full, depth + 1)
            else:
                out.append(f"{indent}{name}")
            if len(out) >= max_items:
                return

    walk(workspace, 0)
    return out


def get_project_context(workspace: str) -> str:
    """生成项目概览文本；工作区无效时返回 ""。"""
    if not workspace or not os.path.isdir(workspace):
        return ""
    ws = os.path.normpath(workspace)
    now = time.time()
    cached = _last_cache.get(ws)
    if cached and now - cached["ts"] < _CACHE_TTL:
        return cached["text"]

    sections: list[str] = []

    # 约定文件
    agents = ""
    for fname in _AGENTS_FILES:
        if os.path.isfile(os.path.join(ws, fname)):
            agents = _read_first(os.path.join(ws, fname), 4000)
            break
    if agents:
        safe = agents.strip()[:3000]
        sections.append(f"【项目约定 AGENTS/README】\n{safe}")

    # 目录树
    tree = _tree(ws)
    if tree:
        tree_txt = "\n".join(tree)
        sections.append(f"【项目结构】\n{tree_txt}")

    text = "\n\n".join(sections)
    _last_cache[ws] = {"ts": now, "text": text}
    return text
