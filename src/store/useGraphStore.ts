import { create } from 'zustand';
import type { AppendMode, DagNode, FilterStatus } from '../types';
import { api, toDagNode, type ContextEntryOut } from '../api';
import { connectWs } from '../ws';

// ---- 执行事件(exec_event)总线：后端 WS 实时推送执行轨迹，组件订阅刷新 ----
type ExecListener = (e: any) => void;
const _execListeners = new Set<ExecListener>();
export function onExecEvent(cb: ExecListener): () => void {
  _execListeners.add(cb);
  return () => { _execListeners.delete(cb); };
}
function emitExecEvent(e: any): void {
  for (const cb of _execListeners) { try { cb(e); } catch { /* ignore */ } }
}

export const CONCURRENCY = 5;
const COLLAPSED_KEY = 'dag-card-collapsed';

function loadCollapsed(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem(COLLAPSED_KEY) ?? '{}');
  } catch {
    return {};
  }
}

interface GraphState {
  nodes: Record<string, DagNode>;
  rootId: string;
  sessionId: string;
  sessionTitle: string;
  sessions: { sessionId: string; title: string | null; workspace: string | null; createdAt: string; updatedAt: string }[];
  approvalsByNode: Record<string, { approvalId: string; tool: string; args: any }>;
  settingsOpen: boolean;
  availableModels: { provider: string; label: string; model: string }[];
  sessionUsage: { promptTokens: number; completionTokens: number; cost: number; budget: number | null; balance: number | null };
  contextVersion: number;
  contextEntries: ContextEntryOut[];

  appendMode: AppendMode;
  inputText: string;
  anchorIds: string[];
  selectingSource: boolean;
  sourceIds: string[];
  filterStatus: FilterStatus;
  filterKeyword: string;
  collapsed: Record<string, boolean>;
  fitNonce: number;
  focusNonce: number;
  focusRequest: { nodeId: string; zoomIn: boolean; nonce: number } | null;
  openWindows: Record<string, { x: number; y: number; width: number; height: number }>;
  windowZ: Record<string, number>;
  connected: boolean;

  newSession: (title?: string, workspace?: string) => Promise<void>;
  loadSession: (sid: string) => Promise<void>;
  loadSessions: () => Promise<void>;
  deleteSession: (sid: string) => Promise<void>;
  loadAvailableModels: () => Promise<void>;
  loadSessionUsage: () => Promise<void>;
  setNodeModel: (id: string, model: string) => Promise<void>;
  uploadNodeFile: (id: string, file: File) => Promise<void>;
  setSessionBudget: (budget: number | null) => Promise<void>;
  approveApproval: (aid: string, nodeId: string) => Promise<void>;
  rejectApproval: (aid: string, nodeId: string) => Promise<void>;
  setSettingsOpen: (open: boolean) => void;
  resolveContext: (key: string, action: 'keep_first' | 'keep_second' | 'merge', mergeValue?: string) => Promise<void>;
  refreshGraph: () => Promise<void>;
  setAppendMode: (m: AppendMode) => void;
  setInputText: (t: string) => void;
  clickNode: (id: string) => void;
  clearAnchors: () => void;
  submitAppend: () => Promise<void>;
  startNewCard: () => void;
  toggleSource: (id: string) => void;
  confirmNewCard: () => Promise<void>;
  cancelNewCard: () => void;
  quickCreate: (id: string, mode: AppendMode) => Promise<void>;
  toggleCollapsed: (id: string) => void;
  toggleSubtree: (id: string) => void;
  cancelNode: (id: string) => Promise<void>;
  retryNode: (id: string) => Promise<void>;
  removeNode: (id: string) => Promise<void>;
  resolveBlocked: (id: string, action: 'skip' | 'cancel') => Promise<void>;
  pauseNode: (id: string) => Promise<void>;
  resumeNode: (id: string) => Promise<void>;
  moveNode: (id: string, dx: number, dy: number) => void;
  resizeNode: (id: string, width: number, height: number) => void;
  updateNodeTitle: (id: string, title: string) => Promise<void>;
  updateNodeInput: (id: string, text: string) => Promise<void>;
  sendMessage: (id: string, text: string) => Promise<void>;
  focusAppend: () => void;
  requestFocusNode: (nodeId: string, zoomIn: boolean) => void;
  toggleWindow: (nodeId: string, rect: { x: number; y: number; width: number; height: number }) => void;
  raiseWindow: (nodeId: string) => void;
  moveWindow: (nodeId: string, x: number, y: number) => void;
  resizeWindow: (nodeId: string, width: number, height: number) => void;
  setFilterStatus: (s: FilterStatus) => void;
  setFilterKeyword: (k: string) => void;
  requestFit: () => void;
  tidyCanvas: () => void;
  applyStatusEvent: (event: any) => Promise<void>;
}

export const useGraphStore = create<GraphState>()((set, get) => {
  let wsCleanup: ({ close: () => void } | null) = null;

  function clearWs() {
    if (wsCleanup) {
      wsCleanup.close();
      wsCleanup = null;
    }
  }

  function upsertNode(nodes: Record<string, DagNode>, node: DagNode) {
    return { ...nodes, [node.nodeId]: node };
  }

  return {
    nodes: {},
    rootId: '',
    sessionId: '',
    sessionTitle: '',
    sessions: [],
    availableModels: [],
    sessionUsage: { promptTokens: 0, completionTokens: 0, cost: 0, budget: null, balance: null },
    approvalsByNode: {},
    settingsOpen: false,
    contextVersion: 0,
    contextEntries: [],

    appendMode: 'serial',
    inputText: '',
    anchorIds: [],
    selectingSource: false,
    sourceIds: [],
    filterStatus: 'all',
    filterKeyword: '',
    collapsed: loadCollapsed(),
    fitNonce: 0,
    focusNonce: 0,
    focusRequest: null,
    openWindows: {},
    windowZ: {},
    connected: false,

    newSession: async (title, workspace) => {
      clearWs();
      const root = await api.createSession(title, workspace);
      const nodes: Record<string, DagNode> = { [root.nodeId]: toDagNode(root) };
      set({
        nodes,
        rootId: root.nodeId,
        sessionId: root.sessionId,
        sessionTitle: title?.trim() || '',
        anchorIds: [root.nodeId],
        inputText: '',
        selectingSource: false,
        sourceIds: [],
        fitNonce: get().fitNonce + 1,
      });
      attachWs(root.sessionId);
      await get().loadSessions();
    },

    loadSession: async (sid) => {
      clearWs();
      const graph = await api.getGraph(sid);
      const nodes: Record<string, DagNode> = {};
      const rootNode = graph.nodes.find((n) => n.kind === 'root');
      for (const n of graph.nodes) nodes[n.nodeId] = toDagNode(n);
      set({
        nodes,
        rootId: rootNode?.nodeId ?? (graph.nodes[0]?.nodeId ?? ''),
        sessionId: sid,
        sessionTitle: rootNode && rootNode.title !== '会话起点' ? rootNode.title : '',
        anchorIds: rootNode ? [rootNode.nodeId] : [],
        inputText: '',
        selectingSource: false,
        sourceIds: [],
        fitNonce: get().fitNonce + 1,
      });
      attachWs(sid);
      // 拉取共享上下文
      try {
        const ctx = await api.getContext(sid);
        set({ contextVersion: ctx.contextVersion, contextEntries: ctx.entries });
      } catch (e) {
        console.error('加载共享上下文失败', e);
      }
      await get().loadSessionUsage();
    },

    loadSessions: async () => {
      try {
        const sessions = await api.listSessions();
        set({ sessions });
      } catch (e) {
        console.error('加载历史会话失败', e);
      }
    },

    deleteSession: async (sid) => {
      try {
        await api.deleteSession(sid);
        const s = get();
        // 删除后刷新列表；若删除的是当前会话，则加载最近一个；若已无会话则清空画布（不自动新建）
        const sessions = (await api.listSessions()).filter((x) => x.sessionId !== sid);
        set({ sessions });
        if (s.sessionId === sid) {
          clearWs();
          if (sessions.length > 0) {
            await s.loadSession(sessions[0].sessionId);
          } else {
            set({
              nodes: {},
              rootId: '',
              sessionId: '',
              sessionTitle: '',
              anchorIds: [],
              selectingSource: false,
              sourceIds: [],
              approvalsByNode: {},
              contextVersion: 0,
              contextEntries: [],
              sessionUsage: { promptTokens: 0, completionTokens: 0, cost: 0, budget: null, balance: null },
            });
          }
        }
      } catch (e) {
        console.error('删除会话失败', e);
      }
    },

    loadAvailableModels: async () => {
      try {
        const { availableModels } = await api.getModelAvailable();
        set({ availableModels });
      } catch (e) {
        console.error('加载可用模型失败', e);
      }
    },

    loadSessionUsage: async () => {
      const sid = get().sessionId;
      if (!sid) return;
      try {
        const u = await api.getSessionUsage(sid);
        set({ sessionUsage: u });
      } catch (e) {
        /* ignore */
      }
    },

    setNodeModel: async (id, model) => {
      try {
        const node = await api.updateNode(id, { model });
        set((s) => ({ nodes: upsertNode(s.nodes, toDagNode(node)) }));
      } catch (e) {
        console.error('设置模型失败', e);
      }
    },

    uploadNodeFile: async (id, file) => {
      try {
        const node = await api.uploadNodeFile(id, file);
        set((s) => ({ nodes: upsertNode(s.nodes, toDagNode(node)) }));
      } catch (e) {
        console.error('上传文件失败', e);
      }
    },

    setSessionBudget: async (budget) => {
      const sid = get().sessionId;
      if (!sid) return;
      try {
        await api.setSessionBudget(sid, budget);
        await get().loadSessionUsage();
      } catch (e) {
        console.error('设置预算失败', e);
      }
    },

    resolveContext: async (key, action, mergeValue) => {
      const sid = get().sessionId;
      if (!sid) return;
      try {
        await api.resolveContext(sid, key, { action, mergeValue });
        const ctx = await api.getContext(sid);
        set({ contextVersion: ctx.contextVersion, contextEntries: ctx.entries });
      } catch (e) {
        console.error('裁决失败', e);
      }
    },

    approveApproval: async (aid, nodeId) => {
      try {
        await api.approveApproval(aid);
        set((s) => {
          const m = { ...s.approvalsByNode };
          delete m[nodeId];
          return { approvalsByNode: m };
        });
      } catch (e) {
        console.error('审批通过失败', e);
      }
    },

    setSettingsOpen: (open) => set({ settingsOpen: open }),

    rejectApproval: async (aid, nodeId) => {
      try {
        await api.rejectApproval(aid);
        set((s) => {
          const m = { ...s.approvalsByNode };
          delete m[nodeId];
          return { approvalsByNode: m };
        });
      } catch (e) {
        console.error('审批拒绝失败', e);
      }
    },

    refreshGraph: async () => {
      const sid = get().sessionId;
      if (!sid) return;
      const graph = await api.getGraph(sid);
      const nodes: Record<string, DagNode> = {};
      const rootNode = graph.nodes.find((n) => n.kind === 'root');
      for (const n of graph.nodes) nodes[n.nodeId] = toDagNode(n);
      set({
        nodes,
        rootId: rootNode?.nodeId ?? (graph.nodes[0]?.nodeId ?? get().rootId),
      });
      // 兜底：若存在待审批（错过了 WS 事件），按 nodeId 分组写入
      try {
        const pendings = await api.getPendingApprovals(sid);
        if (pendings.length > 0) {
          set((s) => {
            const m = { ...s.approvalsByNode };
            for (const p of pendings) {
              m[p.nodeId] = { approvalId: p.approvalId, tool: p.tool, args: p.args };
            }
            return { approvalsByNode: m };
          });
        }
      } catch {
        /* ignore */
      }
    },

    setAppendMode: (m) =>
      set((s) => {
        if (m === s.appendMode) return {};
        const anchorIds = m === 'join' ? s.anchorIds : s.anchorIds.slice(0, 1);
        return { appendMode: m, anchorIds };
      }),

    setInputText: (t) => set({ inputText: t }),

    clickNode: (id) =>
      set((s) => {
        if (s.selectingSource) {
          const has = s.sourceIds.includes(id);
          return { sourceIds: has ? s.sourceIds.filter((x) => x !== id) : [...s.sourceIds, id] };
        }
        if (s.appendMode === 'join') {
          const has = s.anchorIds.includes(id);
          return { anchorIds: has ? s.anchorIds.filter((a) => a !== id) : [...s.anchorIds, id] };
        }
        return { anchorIds: [id] };
      }),

    clearAnchors: () => set({ anchorIds: [] }),

    startNewCard: () => set({ selectingSource: true, sourceIds: [], anchorIds: [] }),
    toggleSource: (id) =>
      set((s) => {
        if (!s.selectingSource) return {};
        const has = s.sourceIds.includes(id);
        return { sourceIds: has ? s.sourceIds.filter((x) => x !== id) : [...s.sourceIds, id] };
      }),

    confirmNewCard: async () => {
      const s = get();
      const parentIds = s.sourceIds.length > 0 ? [...s.sourceIds] : [];
      const mode: AppendMode =
        parentIds.length > 1 ? 'join' : parentIds.length === 1 ? 'serial' : 'serial';
      try {
        const node = await api.addNode(s.sessionId, { mode, parentIds, input: { text: '' } });
        set((st) => ({
          nodes: upsertNode(st.nodes, toDagNode(node)),
          selectingSource: false,
          sourceIds: [],
          collapsed: { ...st.collapsed, [node.nodeId]: false },
          fitNonce: st.fitNonce + 1,
        }));
      } catch (e) {
        console.error('创建新卡片失败', e);
      }
    },

    cancelNewCard: () => set({ selectingSource: false, sourceIds: [] }),

    quickCreate: async (id, mode) => {
      const s = get();
      const src = s.nodes[id];
      if (!src) return;
      if (mode === 'join') {
        set({ selectingSource: true, sourceIds: [id], anchorIds: [] });
        return;
      }
      let parentIds: string[];
      if (mode === 'parallel') {
        const p = src.parentIds[0];
        parentIds = p ? [p] : [s.rootId];
      } else {
        parentIds = [id];
      }
      try {
        const node = await api.addNode(s.sessionId, { mode, parentIds, input: { text: '' } });
        set((st) => ({
          nodes: upsertNode(st.nodes, toDagNode(node)),
          collapsed: { ...st.collapsed, [node.nodeId]: false },
          fitNonce: st.fitNonce + 1,
        }));
      } catch (e) {
        console.error('快捷创建失败', e);
      }
    },

    submitAppend: async () => {
      const s = get();
      const text = s.inputText.trim();
      if (!text) return;
      let parentIds: string[];
      let mode = s.appendMode;
      if (s.appendMode === 'join') {
        if (s.anchorIds.length < 2) return;
        parentIds = [...s.anchorIds];
      } else if (s.appendMode === 'parallel') {
        const anchor = s.anchorIds[0] ?? s.rootId;
        if (anchor === s.rootId) return;
        const p = s.nodes[anchor]?.parentIds[0];
        if (!p) return;
        parentIds = [p];
      } else {
        parentIds = [s.anchorIds[0] ?? s.rootId];
      }
      try {
        const node = await api.addNode(s.sessionId, { mode, anchorNodeId: s.anchorIds[0], parentIds, input: { text } });
        set((st) => ({
          nodes: upsertNode(st.nodes, toDagNode(node)),
          inputText: '',
          anchorIds: st.appendMode === 'join' ? [] : st.anchorIds,
          fitNonce: st.fitNonce + 1,
        }));
      } catch (e) {
        console.error('追加节点失败', e);
      }
    },

    toggleCollapsed: (id) =>
      set((s) => {
        const collapsed = { ...s.collapsed, [id]: !(s.collapsed[id] ?? true) };
        try {
          localStorage.setItem(COLLAPSED_KEY, JSON.stringify(collapsed));
        } catch {
          /* ignore */
        }
        return { collapsed };
      }),

    toggleSubtree: (id) => {
      const n = get().nodes[id];
      if (!n) return;
      const next = !n.subtreeCollapsed;
      set((s) => ({ nodes: { ...s.nodes, [id]: { ...n, subtreeCollapsed: next } } }));
      api.updateNode(id, { subtreeCollapsed: next }).catch(() => {});
    },

    cancelNode: async (id) => {
      try {
        const node = await api.cancelNode(id);
        set((s) => ({ nodes: upsertNode(s.nodes, toDagNode(node)) }));
      } catch (e) {
        console.error('取消失败', e);
      }
    },

    retryNode: async (id) => {
      try {
        const node = await api.retryNode(id);
        // 原地重试：返回的是同一个节点，直接覆盖本地状态
        set((st) => ({ nodes: upsertNode(st.nodes, toDagNode(node)) }));
      } catch (e) {
        console.error('重试失败', e);
      }
    },

    removeNode: async (id) => {
      const s = get();
      const n = s.nodes[id];
      if (!n || n.kind === 'root') return;
      const hasDownstream = Object.values(s.nodes).some((x) => x.parentIds.includes(id));
      if (hasDownstream) return;
      try {
        await api.deleteNode(id);
        const nodes = { ...s.nodes };
        delete nodes[id];
        set({
          nodes,
          anchorIds: s.anchorIds.filter((a) => a !== id),
          sourceIds: s.sourceIds.filter((a) => a !== id),
        });
      } catch (e) {
        console.error('删除失败', e);
      }
    },

    resolveBlocked: async (id, action) => {
      try {
        const node = await api.resolveBlocked(id, action);
        set((s) => ({ nodes: upsertNode(s.nodes, toDagNode(node)) }));
      } catch (e) {
        console.error('blocked 裁决失败', e);
      }
    },

    pauseNode: async (id) => {
      try {
        const node = await api.pauseNode(id);
        set((s) => ({ nodes: upsertNode(s.nodes, toDagNode(node)) }));
      } catch (e) {
        console.error('暂停失败', e);
      }
    },

    resumeNode: async (id) => {
      try {
        const node = await api.resumeNode(id);
        set((s) => ({ nodes: upsertNode(s.nodes, toDagNode(node)) }));
      } catch (e) {
        console.error('恢复失败', e);
      }
    },

    moveNode: (id, dx, dy) =>
      set((s) => {
        const n = s.nodes[id];
        if (!n) return {};
        const prev = n.dragOffset ?? { dx: 0, dy: 0 };
        const dragOffset = { dx: prev.dx + dx, dy: prev.dy + dy };
        api.updateNode(id, { dragOffset }).catch(() => {});
        return { nodes: { ...s.nodes, [id]: { ...n, dragOffset } } };
      }),

    resizeNode: (id, width, height) => {
      const w = Math.max(260, Math.round(width));
      const h = Math.max(180, Math.round(height));
      const customSize = { width: w, height: h };
      // 拖动过程中只更新本地状态，不在此发请求（拖动结束由调用方持久化）
      set((s) => {
        const n = s.nodes[id];
        if (!n) return {};
        return { nodes: { ...s.nodes, [id]: { ...n, customSize } } };
      });
    },

    updateNodeTitle: async (id, title) => {
      const t = title.trim();
      if (!t) return;
      try {
        const node = await api.updateNode(id, { title: t });
        set((s) => ({ nodes: upsertNode(s.nodes, toDagNode(node)) }));
      } catch (e) {
        console.error('更新标题失败', e);
      }
    },

    updateNodeInput: async (id, text) => {
      try {
        const node = await api.updateNode(id, { input: { text } });
        set((s) => ({ nodes: upsertNode(s.nodes, toDagNode(node)) }));
      } catch (e) {
        console.error('更新输入失败', e);
      }
    },

    sendMessage: async (id, text) => {
      const t = text.trim();
      if (!t) return;
      try {
        // 父子互斥预检：有父或子节点在执行时，提醒用户且不发送
        const cc = await api.checkConcurrency(id);
        if (cc?.blocked) {
          const who = cc.runningTitles?.length ? cc.runningTitles.join('、') : cc.runningRelatives?.join('、') ?? '';
          alert(`⚠ 无法同时执行：${cc.message ?? '有父节点或子节点正在执行'}${who ? `（正在执行：${who}）` : ''}`);
          return;
        }
        const node = await api.sendMessage(id, t);
        set((s) => ({ nodes: upsertNode(s.nodes, toDagNode(node)) }));
      } catch (e) {
        const anyErr = e as any;
        if (anyErr?.response?.status === 409 && anyErr?.response?.detail) {
          alert(`⚠ 无法执行：${anyErr.response.detail}`);
        } else {
          console.error('发送消息失败', e);
        }
      }
    },

    focusAppend: () => set((s) => ({ focusNonce: s.focusNonce + 1 })),

    requestFocusNode: (nodeId, zoomIn) =>
      set((s) => ({ focusRequest: { nodeId, zoomIn, nonce: (s.focusRequest?.nonce ?? 0) + 1 } })),

    toggleWindow: (nodeId, rect) =>
      set((s) => {
        const openWindows = { ...s.openWindows };
        const collapsed = { ...s.collapsed };
        if (openWindows[nodeId]) {
          // 关闭窗口 → 折叠态
          delete openWindows[nodeId];
          collapsed[nodeId] = true;
          try {
            localStorage.setItem(COLLAPSED_KEY, JSON.stringify(collapsed));
          } catch {
            /* ignore */
          }
        } else {
          openWindows[nodeId] = rect;
        }
        return { openWindows, collapsed };
      }),

    raiseWindow: (nodeId) =>
      set((s) => {
        if (!s.openWindows[nodeId]) return {};
        // 给该窗口一个更大的 z 值（用时间戳保证严格递增）
        const z = Math.max(0, ...Object.values(s.windowZ)) + 1;
        return { windowZ: { ...s.windowZ, [nodeId]: z } };
      }),

    moveWindow: (nodeId, x, y) =>
      set((s) => {
        if (!s.openWindows[nodeId]) return {};
        return { openWindows: { ...s.openWindows, [nodeId]: { ...s.openWindows[nodeId], x, y } } };
      }),

    resizeWindow: (nodeId, width, height) =>
      set((s) => {
        if (!s.openWindows[nodeId]) return {};
        return {
          openWindows: {
            ...s.openWindows,
            [nodeId]: {
              ...s.openWindows[nodeId],
              width: Math.max(260, Math.round(width)),
              height: Math.max(180, Math.round(height)),
            },
          },
        };
      }),

    setFilterStatus: (s) => set({ filterStatus: s }),
    setFilterKeyword: (k) => set({ filterKeyword: k }),
    requestFit: () => set((s) => ({ fitNonce: s.fitNonce + 1 })),

    tidyCanvas: () =>
      set((s) => {
        const nodes: Record<string, DagNode> = {};
        for (const [id, n] of Object.entries(s.nodes)) {
          if (n.dragOffset || n.customSize) {
            const { dragOffset, customSize, ...rest } = n;
            void dragOffset;
            void customSize;
            nodes[id] = rest;
            api.updateNode(id, { dragOffset: null, customSize: null }).catch(() => {});
          } else {
            nodes[id] = n;
          }
        }
        return { nodes, fitNonce: s.fitNonce + 1 };
      }),

    applyStatusEvent: async (event) => {
      // WS 推送的节点增量：直接合并 status/progress/messages，不再 GET 全节点
      if (event.type === 'node_update' && event.nodeId) {
        set((s) => {
          const n = s.nodes[event.nodeId];
          if (!n) return {};
          const next = { ...n };
          if (event.status) next.status = event.status;
          if (event.progress !== undefined) next.progress = event.progress;
          if (event.messages !== undefined) next.messages = event.messages;
          if (event.plan !== undefined) next.plan = event.plan;
          return { nodes: { ...s.nodes, [event.nodeId]: next } };
        });
      }
      if (event.type === 'node_status' && event.nodeId && event.status) {
        set((s) => {
          const n = s.nodes[event.nodeId];
          if (!n) return {};
          return { nodes: { ...s.nodes, [event.nodeId]: { ...n, status: event.status } } };
        });
      }
      if (event.type === 'usage') {
        // 会话级用量/费用实时推送
        set((s) => {
          const u = { ...s.sessionUsage };
          if (event.promptTokens !== undefined) u.promptTokens = event.promptTokens;
          if (event.completionTokens !== undefined) u.completionTokens = event.completionTokens;
          if (event.cost !== undefined) u.cost = event.cost;
          u.balance = u.budget != null ? +(u.budget - u.cost).toFixed(6) : null;
          return { sessionUsage: u };
        });
      }
      if (event.type === 'approval' && event.approvalId && event.nodeId) {
        // 按卡片记录待审批（多卡片并发各存各的，互不覆盖）
        set((s) => ({
          approvalsByNode: {
            ...s.approvalsByNode,
            [event.nodeId]: { approvalId: event.approvalId, tool: event.tool, args: event.args },
          },
        }));
      }
      if (event.type === 'exec_event') {
        // 执行轨迹实时事件：广播给订阅组件（ExecTimeline 等）即时刷新
        emitExecEvent(event);
      }
      if (event.type === 'parent_context_updated' && event.nodeId) {
        // 父节点发布新内容块：更新下游节点的父上下文索引
        set((s) => {
          const n = s.nodes[event.nodeId];
          if (!n) return {};
          const parentContext = event.context ?? n.parentContext ?? [];
          return {
            nodes: { ...s.nodes, [event.nodeId]: { ...n, parentContext } },
          };
        });
      }
    },
  };

  function attachWs(sid: string) {
    clearWs();
    if (!sid) return;
    wsCleanup = connectWs(
      sid,
      (ev) => {
        useGraphStore.getState().applyStatusEvent(ev);
      },
      () => {
        // 重连/连接成功后：标记已连接并补拉差异（文档 §6 断线重连 lastEventId 的简化实现）
        useGraphStore.setState({ connected: true });
        useGraphStore.getState().refreshGraph().catch(() => {});
      },
    );
    useGraphStore.setState({ connected: true });
  }
});

export function initApp(): void {
  // 应用启动：只加载历史列表和可用模型；若已有历史会话则载入最近一个。
  // 不自动新建会话：新建会话只能由用户点击左侧栏「＋ 新建会话」触发。
  const st = useGraphStore.getState();
  st.loadAvailableModels();
  st.loadSessions().then(() => {
    const list = useGraphStore.getState().sessions;
    if (list.length > 0) {
      void st.loadSession(list[0].sessionId);
    }
  });
}
