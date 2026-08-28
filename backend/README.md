# 会话任务 DAG 卡片 · 后端（M3）

对应需求文档 §3/§4/§6/§7 的 M3 后端：数据模型 + 接口 + 执行调度。

## 运行

```bash
cd backend
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

API 文档：http://localhost:8000/docs

## 技术栈

- FastAPI + SQLAlchemy + SQLite（单文件 `dags.db`）
- 进程内 DAG 调度器（并发上限 5，依赖驱动）
- WebSocket `/ws/sessions/{id}` 推送节点状态变更

## 接口（对照需求 §6）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/sessions` | 新建会话，返回根节点 |
| GET | `/api/sessions/{id}/graph` | 获取整个 DAG（节点+边） |
| POST | `/api/sessions/{id}/nodes` | 追加节点（serial/parallel/join，环检测） |
| GET | `/api/nodes/{nodeId}` | 节点详情 |
| PATCH | `/api/nodes/{nodeId}` | 更新标题/输入 |
| POST | `/api/nodes/{nodeId}/cancel` | 取消执行 |
| POST | `/api/nodes/{nodeId}/retry` | 重试（创建新节点） |
| POST | `/api/nodes/{nodeId}/resolve-blocked` | blocked 裁决（skip/cancel） |
| WS | `/ws/sessions/{id}` | 状态变更推送 |
