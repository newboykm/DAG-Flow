"""Agent 工具集定义：OpenAI function-calling schema + 敏感度标记。

工具分两类：
- 非敏感（只读）：read_file / list_dir / search_files / grep_content / web_search / web_fetch
- 敏感（需人工审批）：write_file / exec_command
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    # 敏感工具在调用前需要用户在卡片里审批（human-in-the-loop）
    # write_file / exec_command 改为按内容动态判定（见 needs_approval），此处置 False
    requires_approval: bool = False


TOOLS: list[Tool] = [
    Tool(
        name="read_file",
        description="读取工作目录内某个文件的完整内容。参数用相对/绝对路径。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（工作目录内）"},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="write_file",
        description="在工作目录内写入/覆盖一个文件。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "要写入的内容"},
            },
            "required": ["path", "content"],
        },
    ),
    Tool(
        name="list_dir",
        description="列出工作目录内某个目录下的文件/子目录（默认根目录）。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径，默认 '.'"},
            },
            "required": [],
        },
    ),
    Tool(
        name="exec_command",
        description="在工作目录内执行一条命令并返回 stdout/stderr。运行环境为 Windows，已兼容常见类 Unix 命令（ls/wc/tail/grep/cat/find 等会经 Git-bash 执行）。run_in_background=true 时后台运行并返回 job id（适合长命令），之后用 job_output 读结果、job_kill 停止、job_list 查看。改文件建议用 write_file，删除/危险命令需审批。",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"},
                "run_in_background": {"type": "boolean", "description": "是否后台运行并返回 job id（默认 false）"},
            },
            "required": ["command"],
        },
    ),
    Tool(
        name="job_list",
        description="列出所有后台任务（job）及其状态（running/done/failed）。",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="job_output",
        description="读取某个后台任务（job）的当前输出。任务完成后可多次读取。",
        parameters={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "后台任务 id（exec_command run_in_background 返回）"},
            },
            "required": ["job_id"],
        },
    ),
    Tool(
        name="job_kill",
        description="停止某个仍在运行的后台任务（job）。",
        parameters={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "要停止的后台任务 id"},
            },
            "required": ["job_id"],
        },
    ),
    Tool(
        name="search_files",
        description="按文件名模式在工作目录内递归搜索文件（glob）。",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "glob 模式，如 *.py"},
            },
            "required": ["pattern"],
        },
    ),
    Tool(
        name="grep_content",
        description="在工作目录内递归搜索文件内容（正则）。",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "glob": {"type": "string", "description": "可选文件过滤，如 *.py"},
            },
            "required": ["pattern"],
        },
    ),
    Tool(
        name="read_parent_output",
        description=(
            "读取上游（父）节点的完整产出内容。参数 parent_title 填上游卡片标题，"
            "block_no 可选（不填则返回该上游节点全部内容块按时间顺序的全文）。"
            "当摘要索引不够、需要上游的完整任务/对话内容时调用本工具。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "parent_title": {"type": "string", "description": "上游节点标题（目录索引里的名字）"},
                "block_no": {"type": "integer", "description": "可选，指定第几块（1 起），不填返回全部"},
            },
            "required": ["parent_title"],
        },
    ),
    Tool(
        name="search_parent_memory",
        description=(
            "在上游（父）节点的历史产出中做语义检索，返回与 query 最相关的若干内容片段（RAG）。"
            "当需要上游的某个细节、但不想读全部全文时调用本工具。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词/问题"},
                "top_k": {"type": "integer", "description": "返回条数，默认 4"},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="web_search",
        description=(
            "联网搜索，返回 DeepSeek 综合答案 + 权威来源列表。"
            "queries 为 1-4 个查询（可包含不同角度/关键词），系统会并行搜索并合并去重，"
            "DeepSeek 会针对查询给出高质量综合回答并附来源 URL。"
            "对重要/易错/需要多方求证的信息，请提供多个不同关键词的查询以获得交叉验证。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "1-4 个搜索查询；多个不同角度可获得更全面、可交叉验证的结果",
                },
                "query": {"type": "string", "description": "兼容旧的单查询写法；已用 queries 时可省略"},
                "depth": {"type": "string", "enum": ["basic", "deep"], "description": "旧参数；native 搜索默认高质量，保留兼容", "default": "basic"},
            },
            "required": [],
        },
    ),
    Tool(
        name="web_fetch",
        description="获取某个 URL 的网页内容。",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要获取的网页 URL"},
            },
            "required": ["url"],
        },
    ),
    Tool(
        name="remember",
        description="保存一条长期记忆（跨会话、跨卡片可用，覆盖同名）。type: user=用户偏好 / feedback=工作反馈 / project=项目目标 / reference=外部资源。scope: project=当前项目 / global=所有项目。",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "记忆名（简短英文标识）"},
                "type": {"type": "string", "description": "记忆类型：user/feedback/project/reference"},
                "scope": {"type": "string", "description": "作用域：project 或 global"},
                "body": {"type": "string", "description": "记忆正文（建议含 **Why:** 与 **How to apply:** 说明原因和适用方式）"},
            },
            "required": ["name", "body"],
        },
    ),
    Tool(
        name="memory_search",
        description="搜索/读取已有长期记忆（关键词匹配 name/type/正文，返回摘要列表）。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词，空则列出全部"},
            },
            "required": [],
        },
    ),
    Tool(
        name="forget",
        description="删除一条长期记忆（当它已过时/错误/被取代时）。",
        parameters={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "作用域：project 或 global"},
                "name": {"type": "string", "description": "要删除的记忆名"},
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="create_goal",
        description="为当前任务设定一个持续目标（objective）。当用户的任务是长期/多轮目标、需要持续实现时使用；agent 会在后续多轮执行中始终记住并朝这个目标推进。简单单任务不用。",
        parameters={
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "具体的完成目标（从用户的直接请求推断）"},
            },
            "required": ["objective"],
        },
    ),
    Tool(
        name="schedule_create",
        description="在会话/卡片里创建一个定时提醒。到点后它会作为一条新的用户消息投递进来，触发下一次执行（可用于定时重跑、延时推进、周期提醒）。prompt 是要在到点时呈现的内容。after_seconds≈间隔秒数，every_seconds≈固定周期秒数（至少300）。",
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "到点时呈现的提醒内容"},
                "after_seconds": {"type": "number", "description": "多少秒后触发（正安全整数）"},
                "every_seconds": {"type": "number", "description": "固定间隔触发（至少 300 秒）"},
            },
            "required": ["prompt"],
        },
    ),
    Tool(
        name="get_goal",
        description="读取当前任务的持续目标。",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="ask_user",
        description="当你在推进中需要用户确认、做选择、或缺少关键信息无法继续时，向用户提出一个或多个问题。用这个工具在严重分歧/关键决策前打断，不要自己猜或憋着。",
        parameters={
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string", "description": "要向用户问的具体问题"},
                            "header": {"type": "string", "description": "可选的简短标题，如「确认」「选择模式」"},
                            "options": {"type": "array", "items": {"type": "string"}, "description": "可选的候选选项，推荐项放第一个并标注 (推荐)"},
                        },
                        "required": ["question"],
                    },
                    "description": "要问用户的问题列表",
                },
            },
            "required": ["questions"],
        },
    ),
    Tool(
        name="session_event_search",
        description="在会话的执行事件日志里做全文搜索（对齐 dsh session_event_search），追溯之前为什么这么做、做过哪些工具调用。可选指定 session_id 跨会话；缺省搜当前会话。返回命中事件的 seq+类型+摘要。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要搜索的文本"},
                "session_id": {"type": "string", "description": "目标会话 id（缺省用当前会话）"},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="session_event_read",
        description="读取某会话执行事件中指定 seq 的完整内容（对齐 dsh session_event_read），用于查看工具调用的完整参数/结果。",
        parameters={
            "type": "object",
            "properties": {
                "seq": {"type": "integer", "description": "事件序列号"},
                "session_id": {"type": "string", "description": "目标会话 id（缺省用当前会话）"},
                "node_id": {"type": "string", "description": "可选，限定某节点的事件"},
            },
            "required": ["seq"],
        },
    ),
    Tool(
        name="skill",
        description="加载一个可用 skill 的完整指令。当任务与某个已知 skill 名称匹配时调用它（参数用上面 Skill 清单里列出的精确名称），以获得按该 skill 约定执行的完整说明。",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "要加载的 skill 名称（来自可用 skill 清单）"},
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="todo_write",
        description="记录并更新当前任务的结构化待办清单。每次调用发送【完整】清单——它会整份替换之前的清单（无局部更新）。开始多步任务前，先为每个具体步骤加一个 todo；正在进行的标记 in_progress（真正并行的可多个同时 in_progress，串行保持一个）；完成的当下就标 completed；全部完成后才允许没有 in_progress 项。简单单步任务可不用。status: pending(未开始)/in_progress(进行中)/completed(已完成)。",
        parameters={
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "该步骤的简短描述（祈使句）"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"], "description": "pending/in_progress/completed"},
                        },
                        "required": ["content", "status"],
                    },
                    "description": "完整的新任务清单（整份替换）",
                },
            },
            "required": ["todos"],
        },
    ),
    Tool(
        name="run_subagent",
        description="把一个完全独立的子任务委托给子代理（它在独立的上下文里工作，不消耗也不共享你的对话）。给它完整、自包含的任务说明（它看不到你的其它对话）。它返回最终结果而非中间步骤。适合调研/局部实现/分析等聚焦工作；复杂任务拆成多个子任务依次/并行调用。",
        parameters={
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "子任务的简短描述（3-5 词，用于展示）"},
                "task": {"type": "string", "description": "完整、自包含的子任务说明（含它需要的全部上下文，因为它看不到你的对话）"},
                "max_rounds": {"type": "integer", "description": "工具调用轮数上限，默认 4"},
            },
            "required": ["task"],
        },
    ),
    Tool(
        name="run_python",
        description=(
            "在工作目录内执行一段 Python 代码并返回 stdout/stderr。用于计算、解析、快速验证、数据加工等；"
            "注意：代码在受控沙箱中运行，无法联网、无法访问工作目录外路径。写文件/删文件等操作请用 write_file 或显式属于敏感命令。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "要执行的 Python 代码"},
            },
            "required": ["code"],
        },
    ),
    Tool(
        name="read_image",
        description=(
            "读取工作目录内的图片/二进制文本文件，返回其内容概要（UTF-8 可读部分；若为图片，返回文件大小与是否为图片等元信息，"
            "暂不提供视觉识别）。主要用于判断文件是否存在、类型与大小。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="str_replace_editor",
        description="代码/文本文件的精确编辑工具（对齐 Claude/SWE 的 str_replace_editor）。命令：view(带行号查看文件/列目录)、create(创建文件)、str_replace(精确字面替换，old_str 须唯一)、insert(在指定行后插入)。状态跨调用。改文件前请先用 view。",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "enum": ["view", "create", "str_replace", "insert"], "description": "要执行的命令"},
                "path": {"type": "string", "description": "文件或目录路径"},
                "file_text": {"type": "string", "description": "create 时的新文件内容"},
                "old_str": {"type": "string", "description": "str_replace 要替换的旧文本（须唯一精确匹配）"},
                "new_str": {"type": "string", "description": "str_replace 的新文本 / insert 要插入的文本"},
                "insert_line": {"type": "integer", "description": "insert 时，在第几行之后插入"},
                "view_range": {"type": "array", "items": {"type": "integer"}, "description": "view 时显示的行号范围，如 [11,12] 或 [11,-1]"},
            },
            "required": ["command", "path"],
        },
    ),
    Tool(
        name="read",
        description="读取 UTF-8 文本文件并返回带行号的内容。用 read 而不是 shell 命令（如 cat）。大文件用 offset/limit 分页继续读。",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "要读取的文件路径（工作目录内，相对/绝对）"},
                "offset": {"type": "number", "description": "起始行号（1 起），默认 1"},
                "limit": {"type": "number", "description": "最多返回行数，默认 2000"},
            },
            "required": ["file_path"],
        },
    ),
    Tool(
        name="edit",
        description="对已存在的 UTF-8 文本文件做精确字面替换。old_string 必须与文件内容完全一致；默认（replace_all=false）要求只出现一次。若出现多次，请提供更精确的 old_string 或设 replace_all=true。编辑前应先使用 read 读取该文件（除非本会话刚创建/编辑过它）。",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "要编辑的文件路径（工作目录内）"},
                "old_string": {"type": "string", "description": "要替换的字面文本，必须精确匹配"},
                "new_string": {"type": "string", "description": "替换文本；空字符串表示删除该匹配"},
                "replace_all": {"type": "boolean", "description": "是否替换所有匹配；默认 false，false 时 old_string 必须恰好出现一次"},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    ),
]

TOOL_BY_NAME: dict[str, Tool] = {t.name: t for t in TOOLS}


# 免审批的「只读/查询」命令白名单（前缀匹配，严格模式）
_READONLY_COMMANDS = (
    "echo", "type", "cat", "dir", "ls", "where", "whoami", "cd", "ver",
    "git status", "git log", "git diff", "git show", "git branch", "git remote -v",
    "python --version", "python -v", "py --version", "node --version", "npm --version",
    "pip --version", "pip list", "pip show",
    "set", "printenv",
)
# 明显危险的命令前缀（写/删除/安装/推送/关机等）→ 必须审批
_DANGEROUS_COMMANDS = (
    "del ", "erase ", "rm ", "rmdir ", "format ", "mkfs",
    "pip install", "pip uninstall", "npm install", "npm uninstall",
    "git push", "git commit", "git reset", "git clean",
    "shutdown", "reboot", "taskkill", "kill", "reg", "net user", "net stop",
    "curl", "wget", "move ", "copy ", "xcopy", "robocopy", "ren ",
    "start ", "schtasks", "powershell", "cmd /c",
)


def command_needs_approval(command: str) -> bool:
    """判断 shell 命令是否需要人工审批（只读/查询类免审，其余审批）。"""
    cmd = (command or "").strip()
    if not cmd:
        return False
    lower = cmd.lower()
    # 危险命令黑名单优先
    for d in _DANGEROUS_COMMANDS:
        if lower.startswith(d):
            return True
    # 只读/查询白名单免审
    for ok in _READONLY_COMMANDS:
        if lower.startswith(ok):
            return False
    # 其它未知命令一律审批（保守）
    return True


def _trust_level() -> str:
    try:
        from .trust import get_trust_level
        return get_trust_level()
    except Exception:
        return "partial"


_IMMUTABLE_TOOLS = {
    # 这些只读/查询工具，即使"全部不信任"也不该审批（不影响工作区）
    "read_file", "read", "list_dir", "search_files", "grep_content",
    "web_search", "web_fetch", "read_parent_output", "search_parent_memory",
    "memory_search", "read_memory", "get_goal", "job_list", "job_output",
    "remember", "skill",
}


def needs_approval(name: str, args: dict) -> bool:
    """按信任档判定是否需人工审批。

    - 只读/查询工具：任何档都不审批。
    - all（全部信任）：写/编辑/命令/运行代码都免审。
    - partial（部分信任，默认）：危险命令、运行代码需审；普通写/编辑免审。
    - none（全部不信任）：写/编辑/任何命令/运行代码都要审。
    """
    if name in _IMMUTABLE_TOOLS:
        return False
    level = _trust_level()
    if level == "all":
        return False  # 全部信任：所有敏感操作免审
    if name == "exec_command":
        dangerous = command_needs_approval(args.get("command", ""))
        return True if level == "none" else dangerous
    if name == "run_python":
        return True  # 能执行任意代码：partial/none 都需审
    # write_file / edit 等写操作：
    if level == "none":
        return True
    return False  # partial：普通写文件/编辑免审


def openai_tools() -> list[dict]:
    """转成 OpenAI 的 tools 参数格式。"""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in TOOLS
    ]
