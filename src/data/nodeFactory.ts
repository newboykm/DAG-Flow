import type { AppendMode, ChatMessage, DagNode, NodeStatus, OutputData } from '../types';

let seq = 0;

export function nextId(prefix = 'n'): string {
  seq += 1;
  return `${prefix}-${seq.toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
}

export function newRoot(sessionId: string): DagNode {
  const now = Date.now();
  return {
    nodeId: 'root',
    sessionId,
    parentIds: [],
    title: '会话起点',
    status: 'done',
    kind: 'root',
    input: { text: '' },
    messages: [],
    createdAt: now,
    updatedAt: now,
    meta: {},
  };
}

export interface MakeTaskOpts {
  title: string;
  parentIds: string[];
  sessionId: string;
  status?: NodeStatus;
  mode?: AppendMode;
  inputText?: string;
  failOnFinish?: boolean;
  failedReason?: string;
  retryOf?: string;
}

export function makeTask(o: MakeTaskOpts): DagNode {
  const now = Date.now();
  const inputText = o.inputText ?? o.title;
  const node: DagNode = {
    nodeId: nextId(),
    sessionId: o.sessionId,
    parentIds: [...o.parentIds],
    title: o.title,
    status: o.status ?? 'pending',
    kind: 'task',
    input: { text: inputText },
    messages: firstTurn(inputText, now),
    createdAt: now,
    updatedAt: now,
    meta: {
      mode: o.mode,
      failOnFinish: o.failOnFinish,
      failedReason: o.failedReason,
      retryOf: o.retryOf,
    },
  };
  if (node.status === 'done') {
    node.output = generateOutput(node.title);
    node.messages.push({
      id: nextId('m'),
      role: 'assistant',
      text: node.output.summary ?? '已完成',
      at: now + 1,
    });
  }
  return node;
}

/** 首轮对话：用户的一条消息（assistant 回复由执行产生） */
function firstTurn(text: string, at: number): ChatMessage[] {
  if (!text) return [];
  return [{ id: nextId('m'), role: 'user', text, at }];
}

/** 生成一条 assistant 消息的初始占位（流式逐字填充） */
export function newAssistantMessage(): ChatMessage {
  return { id: nextId('m'), role: 'assistant', text: '', streaming: true, at: Date.now() };
}

export interface ExecutionPlan {
  /** 预计总耗时 ms（用于进度条分母；实际可能提前/延后完成） */
  expectedMs: number;
  /** 执行阶段名，第 i 期完成 i+1 个阶段（最后一段日志即失败原因/成功日志） */
  phaseLogs: {
    title: string;
    log: string;
  }[];
}

function slug(s: string): string {
  return s.slice(0, 16).replace(/[^\w\u4e00-\u9fa5]+/g, '-');
}

export function generateOutput(title: string): OutputData {
  return {
    type: 'mixed',
    summary: `已完成「${title}」：产出结论与可复用产物`,
    content: `针对「${title}」的执行结果已发布到共享上下文，下游节点可读取。`,
    codeBlocks: [
      { lang: 'ts', title: '核心片段', code: `const result = await run("${title}");\nreturn result.summary;` },
    ],
    files: [{ path: `workspace/${slug(title)}/result.md`, language: 'markdown' }],
    artifacts: [{ id: nextId('a'), name: `${title} · 报告`, type: 'markdown', size: 2048 }],
  };
}
