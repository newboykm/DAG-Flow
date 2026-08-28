# DAG Flow 环境依赖文档

本文档记录 DAG Flow（会话任务 DAG 卡片系统）运行所需的环境与依赖，供新环境部署参考。

## 一、基础运行环境

| 组件 | 版本 | 说明 |
|---|---|---|
| Node.js | v24.x（实测 v24.19.0） | 前端开发/构建 |
| npm | 11.x（实测 11.17.0） | 前端包管理 |
| Python | 3.12.x（实测 3.12.4） | 后端运行 |
| pip | 24.x | Python 包管理 |

> 前端 npm 源已配置为国内镜像：`https://registry.npmmirror.com/`
> Python 建议使用国内镜像：`https://pypi.tuna.tsinghua.edu.cn/simple`

## 二、前端依赖（`package.json`）

### dependencies（运行时）
| 包 | 版本 | 用途 |
|---|---|---|
| react | ^18.3.1 | UI 框架 |
| react-dom | ^18.3.1 | React DOM 渲染 |
| zustand | ^5.0.2 | 状态管理 |
| elkjs | ^0.10.0 | DAG 图分层自动布局 |

### devDependencies（开发）
| 包 | 版本 | 用途 |
|---|---|---|
| typescript | ^5.7.2 | 类型检查与编译 |
| vite | ^6.0.7 | 构建/开发服务器 |
| @vitejs/plugin-react | ^4.3.4 | Vite React 插件 |
| @types/react | ^18.3.12 | React 类型 |
| @types/react-dom | ^18.3.1 | ReactDOM 类型 |

**安装**：`npm install`
**构建**：`npm run build`（`tsc --noEmit && vite build`）
**开发**：`npm run dev`

## 三、后端依赖（`backend/requirements.txt`）

| 包 | 版本 | 用途 |
|---|---|---|
| fastapi | 0.141.1 | Web 框架 |
| uvicorn[standard] | 0.52.4 | ASGI 服务器（含 WebSocket） |
| sqlalchemy | 2.0.52 | ORM（SQLite） |
| pydantic | 2.13.4 | 数据校验 |
| python-multipart | 0.0.32 | 文件上传解析 |
| httpx | 0.28.1 | HTTP 客户端（LLM 流式调用、网页抓取） |
| chromadb | 0.5.5 | 语义记忆向量库（RAG 检索） |
| fastmcp | 3.4.7 | MCP 客户端（第三方工具生态接入） |

**安装**：`pip install -r requirements.txt`

## 四、语义检索（RAG）附加依赖

这些包**未写入 requirements.txt**（因体积大/可选），但语义检索功能需要：

| 包 | 版本 | 用途 | 是否必需 |
|---|---|---|---|
| sentence-transformers | 5.4.1 | 本地 embedding 模型加载 | 可选（缺失时降级离线哈希 embedding） |
| torch | 2.11.0 | sentence-transformers 底层 | 随 sentence-transformers 安装 |
| huggingface-hub | 1.29+ | 模型缓存/下载 | 随 sentence-transformers |
| tokenizers | 0.22.2 | 分词器 | 随 transformers |

### 本地 embedding 模型
- 模型：`BAAI/bge-small-zh-v1.5`（中文语义，512 维）
- 存放路径：`models/bge-small-zh-v1.5/`（已下载到项目内，无需联网）
- 未安装 sentence-transformers 时，自动降级为「离线哈希 embedding」（零依赖、可用但语义弱）

## 五、外部服务（可选）

| 服务 | 用途 | 配置位置 |
|---|---|---|
| 任意 OpenAI 兼容 LLM API（OpenAI/DeepSeek/智谱…） | 大模型对话/规划/摘要 | 设置 → 大模型厂商 |
| Tavily Search API | 高质量联网搜索（解决免费搜索反爬问题） | 设置 → 联网搜索 |
| MCP Server（stdio，如 filesystem/github） | 第三方工具生态 | 设置 → MCP Server |

## 六、数据与持久化

| 内容 | 位置 |
|---|---|
| SQLite 数据库 | `backend/dags.db` |
| ChromaDB 向量库 | `backend/chroma_store/` |
| 长期记忆（remember/memory/forget） | `.agents/memories/` |
| 内置 skill 定义 | `backend/skills/` |
| 本地 embedding 模型 | `models/bge-small-zh-v1.5/` |

## 七、一键安装脚本

| 脚本 | 用途 |
|---|---|
| `setup.cmd` / `setup.ps1` | 一次下载所有依赖（npm + pip；`setup.ps1 -WithRag` 额外装 RAG 模型） |
| `start-dev.cmd` / `start-dev.ps1` | 一次启动前后端（释放端口 8000/5173，分别开窗） |
| `stop-dev.cmd` / `stop-dev.ps1` | 停止前后端 |

- 首次部署：双击 `setup.cmd`（基础依赖）；如需语义检索再运行 `setup.ps1 -WithRag`。
- 模型下载脚本：`backend/download_model.py`（bge-small-zh-v1.5，经 hf-mirror 分段下载+断点续传）。
- 后端：`python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`（在 `backend/` 目录）
- 前端：`npm run dev`（Vite，访问 `http://localhost:5173`，注意用 `localhost` 而非 `127.0.0.1`）
