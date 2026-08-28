---
name: reasonix-guide
description: 排查配置能力：Skills（优先级/发现目录）、Commands（覆盖顺序/命名）、Hooks（11 个事件/自动加载/匹配/超时）、MCP（配置合并/传输/auto_start）、插件包、AGENTS.md 加载顺序。诊断能力加载问题。
---

# Skill: reasonix-guide

自我诊断指南。证据优先，不猜。

## 第一动作
1. 静态能力报告（无网络、不起 MCP 子进程）：`doctor capabilities --json`（桌面版：设置 → Diagnostics）。
2. 仅当用户明确允许起第三方 MCP 时才 live 探测：`--live --timeout 5s --json`。
3. 不要发明自动修复；按报告里的稳定 issue code + 来源 + 修复项来。

## Skills
- 优先级（同名取高）：project > custom > global > builtin；低作用域被 shadowed。
- 发现目录：`.reasonix` / `.agents` / `.agent` / `.claude`；布局 `<name>/SKILL.md` 或 `<name>.md`。
- 症状→修复：缺 → 查 disabled/shadowed/缺少描述/根路径；body 不加载是正常的（按需加载）。

## Commands
- 目录顺序（后者覆盖前者）：home conventions → home → project conventions。
- 命名：`git/commit.md` → `/git:commit`。

## Hooks（11 事件）
- PreToolUse / PostToolUse / PermissionRequest / UserPromptSubmit / Stop / PostLLMCall / SessionStart / SessionEnd / SubagentStop / Notification / PreCompact。
- 阻塞型：PreToolUse、UserPromptSubmit（exit 2 可拦住循环）。match 是锚定正则（`file` 不匹配 `read_file`）。timeout 毫秒。

## MCP
- 合并顺序：TOML [[plugins]] → 项目 .mcp.json → 启用插件包，同名取高。
- 传输：stdio / http / sse。auto_start=false 跳过启动。

## Instructions（AGENTS.md/REASONIX.md/CLAUDE.md）
- 加载顺序（特异性递增）：user global → 祖先链 → 项目 → 项目本地 `*.local.md`。
- 会话启动折叠进 system prompt。

## Safety
- 优先静态诊断；live MCP 可能跑第三方代码和网络。
- 不打印 token/header/env/URL 查询串/用户名/机器绝对路径（用 `<workspace>/…`、`~/…` 表示）。
