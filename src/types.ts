export type NodeStatus =
  | 'pending'
  | 'ready'
  | 'running'
  | 'done'
  | 'failed'
  | 'cancelled'
  | 'paused'
  | 'blocked';

export type NodeKind = 'root' | 'task';
export type AppendMode = 'serial' | 'parallel' | 'join';
export type FilterStatus = NodeStatus | 'all';

export interface CodeBlock {
  lang: string;
  title?: string;
  code: string;
}

export interface FileRef {
  path: string;
  content?: string;
  language?: string;
}

export interface ArtifactRef {
  id: string;
  name: string;
  url?: string;
  type?: string;
  size?: number;
}

/** 卡片内的多轮对话消息（一个卡片 = 一段连续对话） */
export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  text: string;
  /** 该条消息是否仍在流式输出中 */
  streaming?: boolean;
  at: number;
  /** 系统步骤消息（任务流）：调用工具/审批/结果等 */
  step?: string;
  detail?: string;
}

export interface OutputData {
  type: 'text' | 'code' | 'files' | 'mixed';
  content?: string;
  summary?: string;
  codeBlocks?: CodeBlock[];
  files?: FileRef[];
  artifacts?: ArtifactRef[];
}

export interface ProgressStep {
  label: string;
  at: number;
}

/** 执行中间过程（§3.2 progress：步骤、日志、token、耗时等），供前端展开态渲染 */
export interface NodeProgress {
  steps: ProgressStep[];
  logs: string[];
  tokens: number;
  elapsedMs: number;
  /** 总预计耗时（用于进度条） */
  expectedMs: number;
}

export interface NodeMeta {
  mode?: AppendMode;
  runningStart?: number;
  runDurationMs?: number;
  failOnFinish?: boolean;
  failedReason?: string;
  /** 树根（重试链）关联的原节点 id，§4.2.6 重试创建新节点 */
  retryOf?: string;
  /** blocked 「跳过失败继续」时，被跳过的父节点 id（产出传播用） */
  skippedParents?: string[];
}

export interface DagNode {
  nodeId: string;
  sessionId: string;
  parentIds: string[];
  title: string;
  status: NodeStatus;
  kind: NodeKind;
  input: { text: string };
  /** 多轮对话消息流（§卡片内多轮对话） */
  messages: ChatMessage[];
  output?: OutputData;
  progress?: NodeProgress;
  subtreeCollapsed?: boolean;
  /** 用户手动拖动的偏移（覆盖自动布局位置），§后续支持拖拽重排 */
  dragOffset?: { dx: number; dy: number };
  /** 用户手动调整的卡片尺寸（展开态右下角手柄），永久保留 */
  customSize?: { width: number; height: number };
  /** 卡片级选择的大模型 */
  model?: string;
  /** 上传并关联到卡片的文件 */
  files?: { path: string; name: string; size?: number }[];
  /** 父节点上下文索引（父节点发布内容块时后端自动刷新） */
  parentContext?: { parentNodeId: string; parentTitle: string; seq?: number; summary: string }[];
  /** 任务计划（planning 循环生成） */
  plan?: { goal?: string; steps?: { label: string; status: 'pending' | 'running' | 'done' | 'failed' }[] };
  createdAt: number;
  updatedAt: number;
  meta: NodeMeta;
}
