"""工具化记忆（对齐 Reasonix 的 memory/remember/forget）。

- 文件存储：项目根 `.agents/memories/` 下，按 scope/name 存 `*.md`。
- remember: 存一条长期事实（覆盖同名）。
- memory: 搜索/列出/读取记忆。
- forget: 归档（删除）一条记忆。

记忆类型（type）：user（用户偏好）/ feedback（工作方式反馈）/ project（项目目标约束）/ reference（外部资源指针）。
scope：project（当前工作区，默认）| global（所有项目）。
"""
from __future__ import annotations

import os
import re

# 项目根（工作区）= 后端目录的上级
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MEMORY_DIR = os.path.join(_PROJECT_ROOT, ".agents", "memories")


def _ensure_dir() -> str:
    os.makedirs(_MEMORY_DIR, exist_ok=True)
    return _MEMORY_DIR


def _path_for(scope: str, name: str) -> str:
    d = os.path.join(_MEMORY_DIR, scope) if scope != "global" else _MEMORY_DIR
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r"[^\w\-]+", "-", name).strip("-") or "memory"
    return os.path.join(d, f"{safe}.md")


def remember_item(scope: str, name: str, mtype: str, body: str) -> str:
    """保存/更新一条记忆，返回稳定引用。"""
    path = _path_for(scope, name)
    header = ["---", f"scope: {scope}", f"name: {name}", f"type: {mtype}", "---", "", body.strip(), ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(header))
    ref = f"{scope}/{name}.md"
    return ref


def search_memories(query: str, top_k: int = 8) -> list[dict]:
    """按关键词粗检索记忆（命中 name/type/body 行），返回摘要列表。"""
    _ensure_dir()
    out: list[dict] = []
    q = (query or "").lower()
    for root, dirs, files in os.walk(_MEMORY_DIR):
        for fn in files:
            if not fn.endswith(".md"):
                continue
            path = os.path.join(root, fn)
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            name = fn[:-3]
            scope = "global" if os.path.dirname(path) == _MEMORY_DIR else os.path.basename(os.path.dirname(path))
            mtype = ""
            m = re.search(r"^type:\s*(.+)$", text, re.M)
            if m:
                mtype = m.group(1).strip()
            if q and q not in text.lower() and q not in name.lower():
                continue
            out.append(
                {
                    "scope": scope,
                    "name": name,
                    "type": mtype,
                    "path": path,
                    "preview": text.strip().splitlines()[-1][:200] if text.strip() else "",
                }
            )
            if len(out) >= top_k:
                return out
    return out


def read_memory(scope: str, name: str) -> str:
    """读取一条记忆全文。"""
    path = _path_for(scope, name)
    if not os.path.isfile(path):
        return f"记忆不存在：{scope}/{name}"
    return open(path, encoding="utf-8", errors="replace").read()


def forget_item(scope: str, name: str) -> str:
    """归档（删除）一条记忆。"""
    path = _path_for(scope, name)
    if not os.path.isfile(path):
        return f"记忆不存在：{scope}/{name}"
    os.remove(path)
    return f"已删除记忆 {scope}/{name}"
