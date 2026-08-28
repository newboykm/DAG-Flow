---
name: install-capability
description: 从 URL、GitHub/raw 文件、本地路径/目录、.mcp.json、可执行文件或包名安装/卸载 MCP server 和 skill。先用 apply=false 出计划（含每步风险级别），确认后再 apply=true。
---

# Skill: install-capability

当用户要求从 URL、本地文件/目录、.mcp.json 或包名安装/卸载能力时使用。卸载用同一工具的 op=uninstall。

## 安装流程（像安装器，不像猜 shell 脚本）
1. 从用户请求精确提取来源字符串（https URL / GitHub URL / 本地路径 / .mcp.json / 可执行路径 / npm 包名）。
2. 只在明确时定 kind；不确定用 kind="auto"。
3. 先 apply=false 出计划；用户说 project/global 时带上 scope，说 copy/link/register 时带上 mode，否则用 auto。
4. 读返回计划：status 为 blocked/failed 时报告下一步，**不要在工具无法识别 manifest 时照着 README 编命令**。
5. 逐条看 actions[].riskLevel：
   - low → 直接 apply=true；
   - medium → 应用并说明写了什么；
   - high → 先问用户一句再 apply=true（含会发 auth header 的 MCP、eager 级 server、指向项目/家目录外的 link、对已有条目 replace=true）。
6. 计划可接受且确认完毕 → 再调一次 apply=true，**回传计划调用得到的同一 planId**（不匹配会被拒，改主意要重新 apply=false 取新计划）。
7. apply=true 后报告装了什么、存到哪里、当前会话是否可用；优先用 actions[].canonicalPath/installRoot/discoverable/indexed 而不是猜路径。

## 默认
- MCP 默认全局（每个项目可用）；仅项目级 server/token/命令才 scope="project"。
- 含多个 skill 的目录注册为 skill root，不复制。
- 单个 SKILL.md（或 <name>.md / <name>/SKILL.md）复制（除非用户要 link/register）。
- 本地 SKILL.md 的 references/scripts/assets 等兄弟文件要随父目录一起保留。
- 远程 MCP URL 默认 http（除非端点明确 SSE）；包名 MCP 默认 `npx -y <package>`。
- 绝不在 header/config 放明文 token，用 `${VAR}` 占位并告知用户设哪个环境变量。

## 卸载（op=uninstall）
- 同名 + 同 scope 的 op=uninstall（source 忽略）。skill/MCP 匹配发生在所选 scope 的当前配置里；不确定位置就问。删除是破坏性的，但与已批准安装对称，直接应用免二次审批。

## 停手条件
- 来源只是文档页、没有 manifest 的 README，或无法确定安装命令时，停下来问她，不要猜。
