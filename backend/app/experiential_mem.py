"""Experiential Memory（经验/能力型记忆）：自动积累 + 按需回写 + 按需加载。

让 agent 像人一样记住"这个项目/这类任务怎么做事"，避免每次执行都重新遍历工具/代码：
- skills_usage：这次实际用了哪些工具来达成什么（如"读结构用 read；跨文档搜用 grep；联网用 web_search 多 query"）
- entry_points：定位到的入口文件/关键模块/目录约定/关键函数（供下次快速定位，而非每次重扫）
- lessons：踩过的坑、解决方式、注意事项、命名/风格约束

存储：<workspace>/.agents/memories/{skills_usage,entry_points,lessons}.md（每文件一条条带标题的条目，
按需整体读入；写入时做去重与长度收敛，避免无脑堆同质记忆）。
"""
from __future__ import annotations

import os
import re

_ENTRIES_PER_FILE = 40  # 每条文件最多保留的条目数


def _mem_dir(workspace: str) -> str:
    d = os.path.join(workspace, ".agents", "memories")
    os.makedirs(d, exist_ok=True)
    return d


def _file_of(workspace: str, kind: str) -> str:
    return os.path.join(_mem_dir(workspace), f"{kind}.md")


def _read(workspace: str, kind: str) -> list[str]:
    p = _file_of(workspace, kind)
    if not os.path.isfile(p):
        return []
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return [l.strip() for l in f.read().splitlines() if l.strip()]
    except Exception:
        return []


def _key_of(line: str) -> str:
    """做去重索引：去符号、去空白、小写前 60 字。"""
    key = re.sub(r"[^\w\u4e00-\u9fff]+", "", (line or "")).lower()
    return key[:60]


def _write(workspace: str, kind: str, entries: list[str]) -> None:
    # 收敛：标题都去重取保留最新的；超长只保留尾部（更可能是新经验）
    p = _file_of(workspace, kind)
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(entries) + "\n")
    except Exception:
        pass


def record_line(workspace: str, kind: str, line: str) -> bool:
    """追加一条经验（去重），返回是否真的新增。kind ∈ skills_usage/entry_points/lessons。"""
    line = (line or "").strip()
    if not line:
        return False
    entries = _read(workspace, kind)
    k = _key_of(line)
    for existing in entries:
        if existing and _key_of(existing) == k:
            return False  # 已存在，不重复
    entries = entries[-_ENTRIES_PER_FILE + 1:] + [line]
    _write(workspace, kind, entries)
    return True


def record_batch(workspace: str, skills: list[str], entries: list[str], lessons: list[str]) -> dict:
    """批量写三类经验，返回各自新增数。"""
    added = {"skills_usage": 0, "entry_points": 0, "lessons": 0}
    for s in skills or []:
        if record_line(workspace, "skills_usage", s):
            added["skills_usage"] += 1
    for e in entries or []:
        if record_line(workspace, "entry_points", e):
            added["entry_points"] += 1
    for l in lessons or []:
        if record_line(workspace, "lessons", l):
            added["lessons"] += 1
    return added


async def reflect_and_sink(
    workspace: str,
    task_title: str,
    tools: list[str],
    changed_files: list[str],
    base_url: str,
    api_key: str,
    model: str,
) -> int:
    """过滤式收获沉淀：任务完成后让模型反思本次可复用经验，过滤后写项目记忆。

    审慎口径（喂给模型的规范）：
    - 只写"可验证、这次确实有用、下回同类还适用"的普适经验；
    - 禁止：臆测、单次偶然因果（如"改了X所以通过"这类未证实）、主观看法（"我建议/我觉得"）、
      不能被下回直接引用的空洞话（"要注意细节"）。
    - 每条约 30-60 字，输出格式每行 `类型|内容`，类型 ∈ {skill(工具/流程复用), entry(关键入口/文件), pitfall(踩坑/对策)}。
    返回写入条数。模型不可用/失败静默返回 0（不影响主流程）。
    """
    if not tools and not changed_files:
        return 0
    from .executor import _chat_once_text
    prompt = (
        "你在为一个失败的/或成功的代码任务沉淀少量<可复用>经验，供未来的同类任务少走弯路。\n"
        "任务：{title}\n"
        "本次用到的工具：{tools}\n"
        "本次涉及的代码文件：{files}\n\n"
        "请提炼最多 4 条真正值得记住的普适经验，每行格式 `类型|内容`：\n"
        "- skill|：该场景用哪个流程/工具/子代理更高效（如\"跨目录找用法用 grep + read 并行\"）\n"
        "- entry|：哪个关键入口/文件/函数值得记住，及其作用（如\"配置入口在 config/env.py\"）\n"
        "- pitfall|：踩过的坑与做法（\"改排序要写回源数组而非只读新建\"）\n"
        "严守：只写这次确实出现、下回同类可用、你能负责任地说出的硬经验；不要臆测、不要\"建议/我觉得\"、"
        "单次偶然结论不算。如果的确没有值得沉淀的，输出一行 `none`。\n"
    ).format(
        title=(task_title or "任务"),
        tools="、".join(tools or ["（未调用工具）"]),
        files="、".join(changed_files or ["（未涉及文件）"]),
    )
    try:
        msgs = [
            {"role": "system", "content": "你是严格的经验沉淀器，只输出可信、可复用的短条目。"},
            {"role": "user", "content": prompt},
        ]
        raw = (await _chat_once_text(base_url, api_key, model, msgs)) or ""
    except Exception:
        return 0
    # 确定性护栏：只收 类型|内容，长度与禁词过滤
    ok_map = {"skill": "skills_usage", "entry": "entry_points", "pitfall": "lessons"}
    wrote = 0
    for line in raw.splitlines():
        line = (line or "").strip()
        if not line or line.lower() == "none":
            continue
        if "|" not in line:
            continue
        tag, _, content = line.partition("|")
        tag = tag.strip().lower()
        content = (content or "").strip()
        if tag not in ok_map or not content:
            continue
        # 过滤主观/臆测/空洞措辞（含对策语气的坑也要去掉这类措辞）
        if any(w in content for w in ("我觉得", "我建议", "建议", "可能", "大概", "也许", "最好")):
            continue
        wrote += 1 if record_line(workspace, ok_map[tag], f"{content}") else 0
    return wrote


def load_project_knowledge(workspace: str) -> str:
    """把项目已积累的经验整理成一段文本，供 build_messages 注入（按需，已掌握的事实）。"""
    parts: list[str] = []
    sk = _read(workspace, "skills_usage")
    ep = _read(workspace, "entry_points")
    ls = _read(workspace, "lessons")
    msgs = {
        "skills_usage": ("【经验·工具用法】本次及此前任务学到的工具/做法（遇到同类任务可照用，不必重新摸索）："),
        "entry_points": ("【经验·入口/关键位置】此前定位到的入口文件、关键模块/目录/函数（可复用快速定位）："),
        "lessons": ("【经验·踩坑与注意】此前踩过的坑、解决方式与本项目约定（规避重蹈）："),
    }
    for kind, lines in (("skills_usage", sk), ("entry_points", ep), ("lessons", ls)):
        if not lines:
            continue
        parts.append(msgs[kind])
        parts.extend("- " + l for l in lines[-12:])  # 只注入最近若干条，避免超长
    return "\n".join(parts) if parts else ""
