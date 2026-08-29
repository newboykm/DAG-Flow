// 后端 REST 封装（对照需求 §6）。所有写操作走这里，读操作也在这里统一。
import type { AppendMode } from './types';

const BASE = '';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`请求失败 ${res.status}: ${text.slice(0, 200)}`);
  }
  return (await res.json()) as T;
}

export interface ApiNode {
  nodeId: string;
  sessionId: string;
  parentIds: string[];
  title: string;
  status: string;
  kind: string;
  input: { text: string };
  messages: { id: string; role: 'user' | 'assistant'; text: string; streaming?: boolean; at: number }[];
  output?: any;
  progress?: any;
  contextRef?: string;
  parentContext?: { parentNodeId: string; parentTitle: string; seq?: number; summary: string }[];
  plan?: { goal?: string; steps?: { label: string; status: 'pending' | 'running' | 'done' | 'failed' }[] } | null;
  collapsed?: boolean | null;
  subtreeCollapsed?: boolean;
  dragOffset?: { dx: number; dy: number } | null;
  customSize?: { width: number; height: number } | null;
  meta?: Record<string, any>;
  model?: string | null;
  files?: { path: string; name: string; size?: number }[];
  createdAt?: string;
  updatedAt?: string;
}

export interface ApiGraph {
  sessionId: string;
  nodes: ApiNode[];
  edges: { id: string; source: string; target: string }[];
}

export interface ContextEntryOut {
  key: string;
  value: string;
  version: number;
  sourceNodeId: string | null;
  status: 'active' | 'conflict';
  candidates: { value: string; sourceNodeId: string | null }[];
}

export const api = {
  createSession: (title?: string, workspace?: string) =>
    request<ApiNode>('/api/sessions', { method: 'POST', body: JSON.stringify({ title: title || null, workspace: workspace || null }) }),

  listSessions: () => request<{ sessionId: string; title: string | null; workspace: string | null; createdAt: string; updatedAt: string }[]>('/api/sessions'),

  deleteSession: (sid: string) => request<{ deleted: string }>(`/api/sessions/${sid}`, { method: 'DELETE' }),

  approveApproval: (aid: string) => request<{ status: string }>(`/api/approvals/${aid}/approve`, { method: 'POST' }),

  rejectApproval: (aid: string) => request<{ status: string }>(`/api/approvals/${aid}/reject`, { method: 'POST' }),

  browseDir: (path: string) =>
    request<{ path: string; parent?: string; entries: { name: string; isDir: boolean; path: string }[]; error?: string }>(
      `/api/browse?path=${encodeURIComponent(path || '')}`,
    ),

  getPendingApprovals: (sid: string) =>
    request<{ approvalId: string; nodeId: string; tool: string; args: any }[]>(
      `/api/sessions/${sid}/approvals/pending`,
    ),

  getSkills: () =>
    request<{ skillDir: string; skills: { name: string; description: string; path: string }[] }>(`/api/skills`),

  saveSkills: (skillDir: string) =>
    request<{ skillDir: string; skills: { name: string; description: string; path: string }[] }>(`/api/skills`, {
      method: 'PUT',
      body: JSON.stringify({ skillDir }),
    }),

  getTavilyConfig: () => request<{ apiKey: string }>(`/api/config/tavily`),

  saveTavilyConfig: (apiKey: string) =>
    request<{ apiKey: string }>(`/api/config/tavily`, { method: 'PUT', body: JSON.stringify({ apiKey }) }),

  listMcpServers: () =>
    request<{ id: number; name: string; command: string; args: string[]; enabled: boolean }[]>(`/api/mcp/servers`),

  addMcpServer: (body: { name: string; command: string; args: string[] }) =>
    request<{ id: number; name: string; command: string; args: string[]; enabled: boolean }>(`/api/mcp/servers`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  deleteMcpServer: (id: number) => request<{ deleted: number }>(`/api/mcp/servers/${id}`, { method: 'DELETE' }),

  toggleMcpServer: (id: number) =>
    request<{ id: number; enabled: boolean }>(`/api/mcp/servers/${id}/toggle`, { method: 'POST' }),

  listMcpTools: () => request<{ tools: { server: string; name: string; description: string; input_schema: any }[] }>(`/api/mcp/tools`),

  getGraph: (sid: string) => request<ApiGraph>(`/api/sessions/${sid}/graph`),

  getContext: (sid: string) => request<{ sessionId: string; contextVersion: number; entries: ContextEntryOut[] }>(`/api/sessions/${sid}/context`),

  resolveContext: (sid: string, key: string, body: { action: 'keep_first' | 'keep_second' | 'merge'; mergeValue?: string }) =>
    request(`/api/sessions/${sid}/context/${encodeURIComponent(key)}/resolve`, { method: 'POST', body: JSON.stringify(body) }),

  getContextFulltext: (sid: string, key: string) =>
    request<{ key: string; summary: string; fulltext: string; sourceNodeId: string | null }>(`/api/sessions/${sid}/context/${encodeURIComponent(key)}/fulltext`),

  addNode: (sid: string, body: { mode: AppendMode; anchorNodeId?: string; parentIds?: string[]; input: { text: string } }) =>
    request<ApiNode>(`/api/sessions/${sid}/nodes`, { method: 'POST', body: JSON.stringify(body) }),

  getNode: (nid: string) => request<ApiNode>(`/api/nodes/${nid}`),

  updateNode: (nid: string, body: Record<string, any>) =>
    request<ApiNode>(`/api/nodes/${nid}`, { method: 'PATCH', body: JSON.stringify(body) }),

  cancelNode: (nid: string) => request<ApiNode>(`/api/nodes/${nid}/cancel`, { method: 'POST' }),

  retryNode: (nid: string) => request<ApiNode>(`/api/nodes/${nid}/retry`, { method: 'POST' }),

  pauseNode: (nid: string) => request<ApiNode>(`/api/nodes/${nid}/pause`, { method: 'POST' }),

  resumeNode: (nid: string) => request<ApiNode>(`/api/nodes/${nid}/resume`, { method: 'POST' }),

  deleteNode: (nid: string) => request<ApiNode>(`/api/nodes/${nid}`, { method: 'DELETE' }),

  resolveBlocked: (nid: string, action: 'skip' | 'cancel') =>
    request<ApiNode>(`/api/nodes/${nid}/resolve-blocked`, { method: 'POST', body: JSON.stringify({ action }) }),

  sendMessage: (nid: string, text: string) =>
    request<ApiNode>(`/api/nodes/${nid}/messages`, { method: 'POST', body: JSON.stringify({ text }) }),

  getModelConfig: () => request<{ hasConfig: boolean; providers: { provider: string; label: string; baseUrl: string; apiKey: string; models: string[]; enabled: boolean }[]; availableModels: { provider: string; label: string; model: string }[] }>('/api/model-config'),

  saveModelProviders: (providers: { provider: string; apiKey: string; baseUrl: string; models: string[] }[]) =>
    request<{ hasConfig: boolean }>('/api/model-config', { method: 'PUT', body: JSON.stringify({ providers }) }),

  getModelAvailable: () => request<{ availableModels: { provider: string; label: string; model: string }[] }>('/api/model-available'),

  getModelPresets: () => request<Record<string, { label: string; base_url: string; models: string[] }>>('/api/model-presets'),

  uploadNodeFile: (nid: string, file: File) => {
    const fd = new FormData();
    fd.append('file', file);
    return fetch(`/api/nodes/${nid}/files`, { method: 'POST', body: fd }).then((r) => {
      if (!r.ok) throw new Error(`上传失败 ${r.status}`);
      return r.json();
    });
  },

  // 图片 OCR：上传图片，返回识别文本（多模态：图片 → 文本 → 模型）
  ocr: (file: File | Blob, filename = 'image.png') => {
    const fd = new FormData();
    fd.append('file', file, filename);
    return fetch('/api/ocr', { method: 'POST', body: fd }).then((r) => {
      if (!r.ok) throw new Error(`OCR 失败 ${r.status}`);
      return r.json() as Promise<{ text: string }>;
    });
  },

  getSessionUsage: (sid: string) =>
    request<{ sessionId: string; promptTokens: number; completionTokens: number; cost: number; budget: number | null; balance: number | null }>(`/api/sessions/${sid}/usage`),

  setSessionBudget: (sid: string, budget: number | null) =>
    request<{ budget: number | null }>(`/api/sessions/${sid}/budget`, { method: 'POST', body: JSON.stringify({ budget }) }),
};

/** 把后端节点转成前端 DagNode（字段对齐） */
export function toDagNode(n: ApiNode): import('./types').DagNode {
  return {
    nodeId: n.nodeId,
    sessionId: n.sessionId,
    parentIds: n.parentIds ?? [],
    title: n.title,
    status: (n.status as import('./types').NodeStatus) ?? 'pending',
    kind: (n.kind as 'root' | 'task') ?? 'task',
    input: n.input ?? { text: '' },
    messages: n.messages ?? [],
    output: n.output,
    progress: n.progress,
    subtreeCollapsed: n.subtreeCollapsed ?? false,
    dragOffset: n.dragOffset ?? undefined,
    customSize: n.customSize ?? undefined,
    model: n.model ?? undefined,
    files: n.files ?? [],
    parentContext: n.parentContext ?? [],
    plan: n.plan ?? undefined,
    createdAt: n.createdAt ? new Date(n.createdAt).getTime() : Date.now(),
    updatedAt: n.updatedAt ? new Date(n.updatedAt).getTime() : Date.now(),
    meta: n.meta ?? {},
  };
}

/** WebSocket 连接已迁移到 src/ws.ts（含断线重连）。 */
