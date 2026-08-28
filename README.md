# 会话任务 DAG 卡片 · M2 前端静态原型

对应需求文档《会话任务DAG卡片_需求设计文档_v1.0.md》的 **M2 前端静态原型**：
卡片/DAG 渲染、展开收起、追加交互（串行/并行/join）、mock 数据。

## 一键启动（推荐）

双击 `start-dev.cmd`（或运行 `powershell -NoProfile -ExecutionPolicy Bypass -File start-dev.ps1`），会自动：

1. 清理 8000/5173 端口；
2. 打开两个独立控制台窗口，分别启动后端（8000）和前端（5173）；
3. 等待服务就绪并打印结果。

- 前端：http://localhost:5173
- 后端 API 文档：http://localhost:8000/docs
- 停止：双击 `stop-dev.cmd`

## 手动启动

```bash
# 终端 1 —— 后端
cd backend
python -m uvicorn app.main:app --reload --port 8000

# 终端 2 —— 前端
npm run dev     # 默认 http://localhost:5173
```

## 构建

```bash
npm install
npm run build   # 类型检查 + 产物构建
npm run preview # 预览构建产物
```

## 技术栈

- 前端：React 18 + TypeScript + Vite + zustand + elkjs（DAG 自动布局，layered/RIGHT，从左到右）+ 自绘 SVG 连线 + HTML 卡片
- 后端：FastAPI + SQLAlchemy + SQLite（`backend/dags.db`），进程内 DAG 调度器 + WebSocket 推送

## 已实现的能力（对照文档章节）

| 能力 | 位置 | 说明 |
|------|------|------|
| DAG 画布 + 从左到右布局 | `FlowCanvas.tsx` / `dagLayout.ts` | elkjs 布局（RIGHT），根节点轻量形态（胶囊） |
| 平移 / 缩放 | `FlowCanvas.tsx` | 拖动空白平移，滚轮以光标为中心缩放 |
| 视口裁剪 | `FlowCanvas.tsx` | 节点 >80 时只渲染可视区（§5.1.1） |
| 卡片两态 | `TaskNodeCard.tsx` | 收起（标题+状态+summary+子任务数）/ 展开（输入/进度/输出/artifacts/操作） |
| 状态色 | `styles.css` | 执行中蓝（呼吸）、完成绿、失败红、阻塞黄、就绪青、等待灰、暂停紫 |
| 锚点选择 | `TaskNodeCard.tsx` | 单击设锚点、双击展开（§5.2 无歧义交互）；右键菜单快捷操作 |
| 全局追加栏 | `GlobalAppendBar.tsx` | 串行/并行/join 三模式 + 输入 + 追加 |
| join 多选 | store `clickNode` | 合并模式连续点选 ≥2 节点作为共同父 |
| 追加规则 | store `submitAppend` | 并行用 `parent(anchor)`、join 用多父；根不可并行 |
| 状态机推进 | store `tick` | pending→ready→running→done/failed，并发上限 5 |
| 执行可视化 | store `tick` + `TaskNodeCard.tsx` | 分步流式步骤/日志、token 统计、进度条、预计/实际耗时 |
| 暂停 / 恢复 | store `pauseNode` / `resumeNode` | running ↔ paused，恢复继续推进 |
| blocked 裁决 | `TaskNodeCard.tsx` | join 父失败 → 下游 blocked，可「跳过失败继续 / 整体取消」 |
| 取消 / 重试 | store | 重试创建新节点（retryOf 关联原节点），原节点保留失败 |
| 子树折叠 | store `toggleSubtree` | 折叠下游为占位，布局按可见节点重排 |
| 过滤 / 搜索 | `FilterBar.tsx` | 按状态筛选 + 关键词搜索（未命中淡出） |
| 演示数据 | `demo.ts` | 小图（含失败→blocked 场景）+ 大图 130+ 节点 |

## 验证清单（对应文档附录 B）

1. 页面默认载入「演示」会话：根节点轻量形态出现。
2. 根下已有串行/并行分支，连线为箭头表示依赖方向。
3. 展开卡片可看到输入/进度/输出；双击卡片切换展开/收起。
4. 合并模式连续点选两个已完成节点 → 输入内容追加 → 新节点两条入边，父均 done 直接 ready→running。
5. 演示图里「汇总测试与性能结论」处于 blocked（父之一 failed），可「跳过失败继续/整体取消」。
6. 点「载入大图（130+）」→ 缩放/平移流畅，搜索/状态过滤可用。

## 目录结构

```
src/
  types.ts                  # 节点/输出/状态类型
  store/useGraphStore.ts    # zustand store + mock 调度器
  layout/dagLayout.ts       # elkjs 布局
  graph/graphUtils.ts       # 拓扑工具（可见节点/childrenMap/初始状态）
  graph/statusMeta.ts       # 状态展示元数据
  data/nodeFactory.ts       # 节点工厂 + 输出生成
  data/demo.ts              # 演示/大图数据生成
  components/               # 画布、卡片、根节点、追加栏、过滤栏、工具栏
```
