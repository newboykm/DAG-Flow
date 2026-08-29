# DAG Flow · 技术文档

> 一个「多卡片 DAG + 多 Agent 自动编排」的工作流系统：每个卡片是一个独立的执行 Agent，卡片间通过有向无环图（DAG）组织成任务链路，上层 Agent 的产出自动传给下层 Agent 作为上下文。

---

## 一、整体架构

```
┌─────────────────────────── 前端（React + Vite）───────────────────────────┐
│  DAG 画布（elkjs 自动布局）│ 多卡片即点即用｜多窗口｜左侧会话/历史 │ 设置弹窗 │
└─────────────────────────▲─────────────────────────────────────────────────┘
                          │ REST / WebSocket（WS 实时增量推送）
┌─────────────────────────┴─────────── 后端（FastAPI）───────────────────────┐
│  DAG 调度 scheduler │ Agent 执行循环（ReAct+Plan+Reflection）              │
│  审批流 human-in-the-loop │ 滚动记忆+剪枝 │ 上下文压缩 │ 项目上下文          │
│  工具系统（15+ 内置 + MCP 动态接入）│ RAG 检索（bge + reranker）            │
│  API Key 加密 │ 多 Key 轮询/重试 │ 预算/计费 │ OCR 多模态                  │
└───────────────────────────────────────────────────────────────────────────┘
   SQLite（业务） · ChromaDB（向量） · 本地 bge/reranker 模型 · 文件记忆
```

---

## 二、前端

### 2.1 框架与技术
| 技术 | 版本 | 用途 |
|---|---|---|
| **React** | 18.3 | UI 框架（函数组件 + Hooks） |
| **TypeScript** | 5.7 | 类型安全，编译期校验 |
| **Vite** | 6.0 | 开发服务器 + 打包（`vite build`，tsc 前置类型检查） |
| **Zustand** | 5.0 | 轻量全局状态管理（画布、节点、会话、WS 事件合并） |
| **ELK.js** | 0.10 | **DAG 图分层自动布局**（节点/连线坐标） |
| **原生 WebSocket** | — | 实时接收后端 node_update / approval / parent_context_updated 增量 |

### 2.2 关键技术点与解决的问题
- **DAG 布局与性能解耦**
  - 问题：卡片拖拽/改大小会频繁触发布局重算，导致卡顿。
  - 解决：elkjs 布局只依赖「拓扑指纹」（节点 id + 父子关系），折叠/展开状态变化才重排；`customSize` 改大小不触发重排（松手才落库）。渲染时折叠态强制用固定尺寸，避免布局尺寸过期造成留白。
- **多窗口 + 点击置顶**
  - 问题：铺满态的卡片需要像窗口一样可移动、可缩放、可切换层级。
  - 解决：`openWindows` 记录窗口 rect，`windowZ` + `raiseWindow` 实现点击置顶；拖动/缩放用 React 合成事件 + `setPointerCapture`，拖动时冻结连线、松手重算。
- **两套连线体系**
  - 画布内是 DAG 边（elkjs 布局）；卡片开窗后源/目标进入窗口层，改用「窗口 rect」实时计算的 `windowEdges`，互不干扰。
- **WS 增量合并**
  - 问题：高频率的 token 级推送如果每次都 GET 全量会导致爆炸。
  - 解决：`applyStatusEvent` 只合并 `status/progress/messages/plan` 增量，本地 diff 更新 store。
- **执行过程展示**
  - 问题：长任务过程不可见、或刷屏混乱。
  - 解决：每轮 =「执行过程折叠块（步骤条 + 工具动作）+ 模型回复」上下两栏；运行时自动展开、完成后收起；卡片处显示「第 x/n 步 · N%」进度。
- **可滚动区域滚轮隔离**
  - 问题：卡片根监听滚轮（滚聊天区/缩放），会抢走子滚动区（执行过程/任务栏）的滚轮。
  - 解决：卡片根 wheel 监听里识别 `.exec-fold-body/.exec-body` 内置滚动区，命中则滚动自身、不接管。
- **多模态输入（图片→OCR）**
  - 聊天框支持粘贴/上传图片，前端调 `/api/ocr` 把识别文字插入输入框再发送。

---

## 三、后端

### 3.1 框架与技术
| 技术 | 版本 | 用途 |
|---|---|---|
| **FastAPI** | 0.141 | Web 框架（REST + WebSocket） |
| **Uvicorn** | 0.52 | ASGI 服务器（含 /ws、WebSocket） |
| **SQLAlchemy 2** | 2.0 | ORM（SQLite 持久化：Node / Session / ContextBlock / Approval 等） |
| **Pydantic** | 2.13 | 数据校验、schemas |
| **httpx** | 0.28 | OpenAI 兼容 LLM 调用、流式、网页抓取 |
| **ChromaDB** | 0.5.5 | 向量库（每个会话一个 collection，内容块做 RAG 召回） |
| **FastMCP** | 3.4.7 | MCP 客户端（stdio，动态接入第三方工具生态；底层 mcp 1.x 规避 uvloop TaskGroup bug） |
| **cryptography.Fernet** | 50 | API Key 对称加密存储 |
| **RapidOCR** | 1.4 | 图片 OCR（多模态输入） |

### 3.2 关键技术点与解决的问题
- **API Key 加密存储**
  - 问题：key 明文落 SQLite，泄露风险高。
  - 解决：`secure.py` 用 Fernet + 本地 `secret.key`；`ModelProvider/ModelConfig` 用 property 透明加解密（DB 存密文、调用方拿明文），启动时自动把存量明文迁移成密文；Tavily key 同样加密。
- **多 Key 轮询与失败重试**
  - 问题：单 key 被同时打爆限流、或 key 挂了全挂。
  - 解决：`api_keys` 支持一个服务商多把 key（逗号/分号/换行分隔），`pick_api_key()` 按服务商轮询均摊；`_chat_once_text` 对 429/5xx/网络错误做指数退避重试。
- **多模型 / 多服务商配置**
  - 卡片级可选模型；`ModelProvider` 表预置 deepseek/智谱/kimi 并支持自加厂商；模型列表动态下发。
- **会话与内容块（ContextBlock / parentContext）**
  - 每个节点按时间追加不可变内容块，下游继承上游全部块索引；`read_parent_output`（取全文）/ `search_parent_memory`（语义检索）按需取用；父节点新增块后经 WS 刷新下游 `parentContext`。
- **审批流（human-in-the-loop）**
  - 敏感工具/命令在 Agent 调用前创建 `Approval` 并弹到前端；`_wait_approval` 轮询（`expire_all` 防 identity-map 缓存）等待批准/拒绝后才继续。
- **命令平台兼容**
  - 问题：Agent 常生成 `wc/tail/ls/grep` 等 Linux 命令，但运行在 Windows。
  - 解决：`_run_command` 检测命令是否类 Unix，若本机有 Git-bash 的 `sh` 则用 `sh -c` 执行，否则用 `cmd /c`。
- **结构化输出校验**
  - 问题：模型返回的 JSON 常带围栏/前后缀/字段类型错。
  - 解决：`jsonutil.py` 统一剥围栏 + 定位最外层 `{}` + 字段类型归一化（`expect_str/list/bool`），失败回退默认值，不静默成功。
- **协作式取消 / 并发调度**
  - 多卡片并发执行；取消通过 DB 状态 + `_cancel_requested` 集合协作式中断（流式每段检查）。
- **OCR / 文件上传**
  - `/api/ocr` 图片识别；`/api/nodes/{nid}/files` 文件关联工作区。

---

## 四、Agent

### 4.1 用什么「框架」实现
Agent 是**自研实现**（不是 LangChain/LangGraph）：以 **ReAct 循环**为核心，叠加 Plan-and-Execute 与 Reflection，运行在异步事件循环里，与工具的审批/流式/WS 推送深度耦合。

> 说明：已评估过 LangGraph，但 LLM 层依赖的 `openai 3.x` 在本机网络连不上 API（httpx2 兼容问题），且 LangGraph 的状态图难以承载「审批挂起、逐 token 流式、随时取消」这类交互式控制流，故保留自研循环。

### 4.2 Agent 用到的技术与解决的问题
- **ReAct 循环（推理 + 工具调用）**
  - 模型每轮可现文本或产生 `tool_calls`；出现工具调用则执行工具、把结果回传（`role:tool`）再继续，直到无工具调用为止。支持最多 25 轮自主推进。
- **Plan-and-Execute（计划驱动）**
  - 开工前 `generate_plan` 用 LLM 把任务拆成 2~6 个步骤；执行中 `plan_index/set_plan_step` 驱动步骤状态（pending→running→done），并把步骤进度推送前端（第 x/n 步）。
- **Reflection（反思重试）**
  - 工具执行失败时注入反思提示让模型换策略重试；同工具失败上限 2 次，防死循环。
- **多个并行/串行维度**
  - 节点支持 `parentIds` 显式关联（串行/并行子任务不依赖过时的 anchor 逻辑）；根节点 `{sid}-root` 避免主键冲突。
- **滚动记忆 + 记忆剪枝 + 层级摘要**
  - `_rolling_memory` 每轮把对话 LLM 折叠成 `{summary, key_facts, conclusion}`；`_prune_memory` 对 key_facts 超 8 条 roll-up、summary 超长下沉为 archive 分层（最多 12 层），防长期记忆无限膨胀。
- **上下文实时压缩**
  - 长对话时 `compact_history` 把「最近 6 条之外的更早历史」用 LLM 压成一条摘要，替代机械截断，解决长对话越聊越笨。
- **项目上下文主动加载**
  - 开工 `project_scan` 扫描工作区读 README/AGENTS.md + 目录树，注入 system prompt，让 Agent 动手前先理解项目（带缓存）。
- **工具系统**
  - 15+ 内置工具（读/写文件、列目录、命令、搜索文件、grep、联网搜索、抓网页、语义检索、记忆三件套、子代理、run_python、read_image 等）+ 白/黑名单审批判定；
  - **MCP 动态接入**：fastmcp 客户端从 `McpServer` 表读启用的 stdio server，工具以 `mcp__{server}__{tool}` 注入每个卡片的 Agent，外部工具生态即插即用。
- **Sub-agent 子代理**
  - 复杂子任务可用 `run_subagent` 派生子代理独立跑（同模型同工具，返回结论）。
- **RAG 检索（智能增强）**
  - 双阶段：bge-small-zh 召回 → `bge-reranker-base` 交叉编码器精排，显著提升 recall 质量；本地模型缺失时自动降级（offline hash embedding / 仅召回）。
- **记忆三件套（memo）**
  - `remember/memory_search/forget`：跨会话、跨卡片的长/短期文件记忆（`.agents/memories/*.md`），含 type/scope 维度。
- **联网搜索降级**
  - 优先 Tavily（answer+urls）→ 失败回退 Bing → 再回退 DuckDuckGo；system prompt 引导"摘要够用直接答、不反复抓取"。

---

## 五、数据与模型

| 类型 | 位置 / 说明 |
|---|---|
| 业务数据 | `backend/dags.db`（SQLAlchemy / SQLite，绝对路径，不随工作目录变化丢数据） |
| 向量库 | `backend/chroma_store/`（每会话一个 collection：`session_{sid}`） |
| 文件记忆 | `.agents/memories/*.md` |
| embedding 模型 | `models/bge-small-zh-v1.5/`（512 维，中文语义） |
| reranker 模型 | `models/bge-reranker-base/`（约 1.1GB，交叉编码器精排） |
| OCR | RapidOCR（内置中英文 onnx 模型，无需系统 tesseract） |
| API Key 密钥 | `.agents/secret.key`（Fernet 密钥文件） |

---

## 六、启动 / 部署

- 一键安装依赖：`setup.cmd`（npm + pip）；`setup.ps1 -WithRag -WithReranker` 装 RAG/精排模型。
- 一键启动：`start-dev.cmd`（后端 8000 + 前端 5173 各开一窗）；停止 `stop-dev.cmd`。
- 后端：`python -m uvicorn app.main:app --port 8000`（backend/ 目录下）。
- 前端：`npm run dev`（Vite，访问 http://localhost:5173，用 localhost 而非 127.0.0.1）。
