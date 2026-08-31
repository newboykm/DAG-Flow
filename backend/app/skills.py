"""Skill 目录扫描与加载（对齐 Reasonix 的 skill 概念）。

- 内置默认 skill 目录：backend/skills（启动即加载）。
- 用户可在前端「设置」里再指定一个自定义 skill 目录（追加到内置之后）。
- 扫描目录：支持 `<目录>/<name>/SKILL.md` 或 `<目录>/<name>.md`。
- 提取 skill 名称 + 描述（正文前几行），注入 agent 的 system prompt。
"""
from __future__ import annotations

import os
import re

# 内置默认 skill 目录（随项目发布，启动即加载）
BUILTIN_SKILL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")


def default_skill_dir() -> str:
    """返回内置默认 skill 目录；不存在则返回 ''。"""
    return BUILTIN_SKILL_DIR if os.path.isdir(BUILTIN_SKILL_DIR) else ""


def list_skills(skill_dir: str) -> list[dict]:
    """扫描 skill 目录，返回 [{name, description, path}]。"""
    if not skill_dir or not os.path.isdir(skill_dir):
        return []
    out: list[dict] = []
    try:
        entries = sorted(os.listdir(skill_dir))
    except Exception:
        return []

    seen = set()
    for e in entries:
        full = os.path.join(skill_dir, e)
        # 子目录形式：<name>/SKILL.md
        if os.path.isdir(full):
            skill_md = os.path.join(full, "SKILL.md")
            if os.path.isfile(skill_md):
                info = _parse_skill_file(skill_md, e)
                if info and info["name"] not in seen:
                    out.append(info)
                    seen.add(info["name"])
        # 平铺形式：<name>.md
        elif e.endswith(".md") and e != "README.md":
            name = e[:-3]
            info = _parse_skill_file(full, name)
            if info and info["name"] not in seen:
                out.append(info)
                seen.add(info["name"])
    return out


def _parse_skill_file(path: str, default_name: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(4000)
    except Exception:
        return None
    # 名称：取 frontmatter 的 name，或标题 # Skill: xxx，或目录名
    name = default_name
    m = re.search(r"^name:\s*[\"']?([\w\-]+)", content, re.M)
    if m:
        name = m.group(1)
    else:
        m = re.search(r"^#\s*(?:Skill:\s*)?(.+)$", content, re.M)
        if m:
            name = m.group(1).strip()
    # 描述：取 "> ..." 引用行，或正文前 200 字
    desc = ""
    m = re.search(r"^>\s*(.+)$", content, re.M)
    if m:
        desc = m.group(1).strip()
    if not desc:
        lines = [l.strip() for l in content.splitlines() if l.strip() and not l.startswith(("#", "```", "---"))]
        desc = " ".join(lines[:3])[:200]
    return {"name": name.strip(), "description": desc, "path": path}


def skills_prompt(skill_dir: str) -> str:
    """生成 skill 目录清单（catalog）文本，注入 system prompt。

    对齐 dsh 的按需加载：system prompt 里只放「技能名 + 一句话描述」（省 token、不稀释注意力），
    并告知 agent：命中某 skill 时调用 skill(name) 工具加载完整指令。
    """
    skills = list_skills(skill_dir)
    if not skills:
        return ""
    lines = ["【可用 Skill（能力清单）】", "任务若与下面某个 skill 匹配，请调用 skill 工具传入其名字加载完整指令后再按它执行："]
    for s in skills:
        lines.append(f"- {s['name']}: {s['description'][:100]}")
    return "\n".join(lines)


def get_skill(name: str, custom_dir: str | None = None) -> dict | None:
    """按名字查找 skill 并返回【完整指令】，供 skill 工具按需加载。

    返回 {name, description, content, path}；找不到返回 None。
    在 (内置, 自定义) 目录里按名字匹配（精确，忽略大小写）。
    """
    name_low = (name or "").strip().lower()
    if not name_low:
        return None
    for d in (default_skill_dir(), custom_dir):
        if not d or not os.path.isdir(d):
            continue
        for s in list_skills(d):
            if s["name"].lower() == name_low:
                try:
                    with open(s["path"], "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    return {"name": s["name"], "description": s["description"], "content": content, "path": s["path"]}
                except Exception:
                    return None
    return None


def load_all_skills_prompt(custom_dir: str | None) -> str:
    """合并内置 skill 目录 + 用户自定义目录，返回注入 prompt 的文本。"""
    parts: list[str] = []
    builtin = default_skill_dir()
    if builtin:
        parts.append(skills_prompt(builtin))
    if custom_dir and os.path.isdir(custom_dir):
        custom = skills_prompt(custom_dir)
        if custom:
            parts.append(custom)
    return "\n\n".join(p for p in parts if p)


def all_skills(custom_dir: str | None = None) -> list[dict]:
    """返回内置 + 自定义 skill 的完整列表（供前端展示）。"""
    seen: dict[str, dict] = {}
    for d in (default_skill_dir(), custom_dir):
        for s in list_skills(d or ""):
            seen.setdefault(s["name"], s)
    return list(seen.values())
