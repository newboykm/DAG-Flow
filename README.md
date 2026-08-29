# DAG Flow · 多卡片 DAG 多 Agent 自动编排系统

一个 **多窗口 DAG 画布 + 多 Agent 自动编排** 的生产级工作流系统：每个卡片是一个独立的智能 Agent，卡片之间用「有向无环图（DAG）」组织成任务链路——上游 Agent 的产出自动传递给下游 Agent 作为上下文，还支持审批、计划、联网/工具、记忆、多模态等能力。

> 想要快速看懂本项目内部实现，可看 [`docs/AGENT_TECH.md`](docs/AGENT_TECH.md)。

---

## 特性一览

- **多卡片 Agent 编排**：每张卡片一个独立 Agent，支持「串行 / 并行 / 合并(join)」三种关系，DAG 自动布局。
- **自主执行**：ReAct 循环 + 计划驱动（Plan-and-Execute）+ 反思重试（Reflection），一次任务尽量自动做完。
- **工具系统**：读/写文件、列目录、执行命令、搜索文件、grep、联网搜索（Tavily/Bing）、抓网页、语义检索、子代理、运行 Python、图片 OCR 等 16+ 工具。
- **MCP 接入**：兼容 MCP 生态（stdio），可接入 filesystem / github 等第三方工具服务器，即插即用。
- **记忆 / RAG**：滚动记忆 + 记忆剪枝 + 层级摘要；本地 embedding（bge）召回 + Reranker 精排，语义检索上游产出。
- **上下文管理**：开工自动读项目背景（README/结构）、长对话自动压缩，避免"越聊越笨"。
- **人工审批**：敏感操作（执行命令、危险命令、运行代码）在执行前弹窗等你确认。
- **多模态输入**：聊天框可直接粘贴/上传**图片**，自动 OCR 提取图中文字交给模型。
- **安全与成本**：API Key 加密存储（Fernet）、多 Key 轮询与限流重试、实时 Token/费用/余额展示、预算设置。
- **多模型**：内置 DeepSeek / 智谱 / Kimi 预设，每张卡片可单独选择模型，也支持自定义厂商。

---

## 环境要求

| 组件 | 建议 |
|---|---|
| Node.js | v18+（实测 v24） |
| Python | 3.10+（实测 3.12） |
| pip | 24+ |
| 操作系统 | Windows（命令执行已做平台兼容；macOS/Linux 亦可） |
| 网络 | pip/npm 需联网装依赖；模型可选择从 hf-mirror 下载 |

---

## 一、安装配置环境

### 方法 A：一键安装（推荐 Windows）

在项目根目录双击 **`setup.cmd`**，会自动：
1. 设置 npm 镜像并安装前端依赖；
2. 用清华镜像安装后端依赖（`pip install -r requirements.txt`，含 API Key 加密、图片 OCR、MCP 等）；
3. 打印完成提示。

如需语义检索（RAG），再运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1 -WithRag          # 装 embedding 模型（~96MB）
powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1 -WithRag -WithReranker   # 再加精排模型（~1.1GB）
```

### 方法 B：手动安装

```bash
# 前端
npm install

# 后端
cd backend
pip install -r requirements.txt

# 可选：语义检索（embedding + 精排模型）
pip install "sentence-transformers==5.4.1" "tokenizers==0.22.2"
python download_model.py            # bge 语义模型
python download_model.py --reranker # bge-reranker 精排（约 1.1GB，可选）
```

---

## 二、启动系统

双击 **`start-dev.cmd`**（或运行 `start-dev.ps1`），会自动清理 8000/5173 端口并分别打开两个控制台窗口启动前后端。

- 前端界面：**http://localhost:5173**
- 后端接口文档：**http://localhost:8000/docs**

手动启动：

```bash
# 终端 1 —— 后端
cd backend
python -m uvicorn app.main:app --reload --port 8000

# 终端 2 —— 前端
npm run dev   # 访问 http://localhost:5173
```

停止：双击 **`stop-dev.cmd`**。

---

## 三、配置你的模型

> ⚠️ 本项目**只存你的 key、不代收**。请在设置里填入你自己的 API Key。

1. 打开网页，点右上角 **「设置」**。
2. 在「大模型厂商」里：
   - 选择预设厂商（DeepSeek / 智谱 / Kimi），填入你自己的 **API Key** 与 **模型名**（每卡片可选模型）；
   - 多把 key 用 **逗号 / 分号 / 换行** 分隔，可自动轮询、限流重试；
   - 也可以「＋ 新增厂商」填 Base URL 接任意 OpenAI 兼容服务。
3. （可选）在「联网搜索」填 **Tavily API Key**，联网搜索会更准（没有则自动回退免费搜索）；
4. （可选）在「MCP Server」添加 stdio 类型的 MCP server（如 `npx -y @modelcontextprotocol/server-filesystem <目录>`），它的工具会自动接入每个卡片。

保存后，新建卡片即可开始。

---

## 四、使用

**基本流程**

1. 左侧「新建会话」→ 填写标题、工作区目录（Agent 只能操作这个目录）。
2. 画布上出现**根卡片** → 点卡片展开，输入你的任务描述，回车发送。
3. 卡片开始自主执行（顶部「执行计划」展示第几步，消息下方有可折叠的「执行过程」）。
4. 需要人工确认的敏感操作会弹审批框：点「允许 / 拒绝」。
5. 结果作为「上下文块」自动传递给下游卡片。

**搭建多 Agent 链路**

- 在卡片上通过右下角 / 底部按钮创建**串行**（下游引用本卡片输出）或**并行**（同层）子卡片；
- 用「合并」把多个已完成卡片作为共同父节点；
- 上游完成后，下游自动继承上游结论作为上下文，可点 🔍 语义检索上游内容。

**交互小技巧**

- 双击卡片：展开 / 收起；缩放时点窗口右上角可固定位置。
- 卡片缩小态：右键菜单可「设为锚点 / 取消执行 / 重试 / 新建卡片 / 删除」。
- 鼠标停在「执行过程」或顶部「任务栏」时，滚轮可在该区域翻动。

---

## 常见问题（FAQ）

- **后端起不来 / 端口占用？** 先用 `stop-dev.cmd` 清理，或检查 8000 端口被占。
- **连接模型失败？** 确认「设置」里 Base URL 正确、API Key 有效（关键在：模型名与你的服务商一致，例如 DeepSeek 填 `deepseek-v4-pro`）。
- **图片识别不好使？** 图片 → OCR 是本地 RapidOCR（装依赖即用）；只识别文字，看不懂图表/流程图。
- **语义检索要联网下载模型？** 已内置到 `models/`（跑 `setup.ps1 -WithRag` 会从 hf-mirror 分段下载）。模型缺失时自动降级为轻量哈希检索。
- **数据存在哪？** 业务在 `backend/dags.db`，向量在 `backend/chroma_store/`，API Key 密钥在 `.agents/secret.key`（勿提交到 GitHub）。

---

## 目录结构

```
DAG Agent/
├─ setup.cmd / setup.ps1     # 一键安装依赖
├─ start-dev.cmd / .ps1      # 一键启动前后端
├─ stop-dev.cmd / .ps1       # 停止
├─ index.html / vite.config.ts
├─ src/                      # 前端（React + TypeScript + Vite + zustand + elkjs）
│  ├─ components/            # 画布、卡片、窗口、设置、审批弹窗、UsageBar 等
│  ├─ store/useGraphStore.ts # 全局状态 + WS 事件合并
│  ├─ api.ts / ws.ts         # 后端接口与 WebSocket
│  └─ types.ts
├─ backend/
│  ├─ requirements.txt       # 后端 Python 依赖
│  ├─ download_model.py      # 下载 embedding / reranker 模型
│  ├─ dags.db                # SQLite（运行生成）
│  └─ app/
│     ├─ main.py             # FastAPI REST + WebSocket
│     ├─ scheduler.py        # DAG 调度 + Agent 执行循环（ReAct/Plan/Reflection）
│     ├─ executor.py         # LLM 调用、上下文压缩、计划
│     ├─ tools.py            # 工具清单与审批判定
│     ├─ tool_executor.py    # 工具执行（命令平台兼容等）
│     ├─ memory.py / memo.py # 语义检索 + 文件记忆
│     ├─ mcp_manager.py      # MCP 客户端（fastmcp）
│     ├─ secure.py           # API Key 加密
│     ├─ jsonutil.py         # 结构化输出校验
│     ├─ project_scan.py     # 开工读取项目背景
│     ├─ ocr.py              # 图片 OCR
│     └─ pricing.py          # 计费价目
├─ models/                   # 本地 bge / reranker 模型（可选下载）
└─ docs/AGENT_TECH.md         # 内部技术文档
```

---

## 技术栈

- **前端**：React 18 · TypeScript · Vite 6 · Zustand · ELK.js · 原生 WebSocket
- **后端**：FastAPI · Uvicorn · SQLAlchemy 2 · SQLite · Pydantic · httpx · ChromaDB · FastMCP · cryptography(拉) · RapidOCR

---

## 免责声明

本项目面向学习与自用，**不收集、不存储你的 API Key 之外的任何账号信息**；Key 仅保存在你本地设备加密存储。模型输出仅供参考，请自行甄别。
