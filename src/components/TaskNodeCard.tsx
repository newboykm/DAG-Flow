import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useGraphStore } from '../store/useGraphStore';
import { STATUS_META } from '../graph/statusMeta';
import { api } from '../api';
import MarkdownBlock from './MarkdownBlock';
import TrustLevelPicker from './TrustLevelPicker';
import ExecTimeline from './ExecTimeline';
import ExecLivePreview from './ExecLivePreview';
import type { AppendMode, DagNode } from '../types';

const MODE_LABEL: Record<AppendMode, string> = { serial: '串行', parallel: '并行', join: 'join' };

interface Props {
  node: DagNode;
  x: number;
  y: number;
  width: number;
  height: number;
  collapsed: boolean;
  isAnchor: boolean;
  dim: boolean;
  sourceSelected?: boolean;
  zoomed?: boolean;
  /** 放大补偿系数（1/scale）：如 2 倍放大传 0.5，铺满画布传 1/k */
  zoomComp?: number;
  childrenCount: number;
  /** 是否渲染在窗口（card-window）内：隐藏自己的 resize 手柄，由窗口层统一处理 */
  inWindow?: boolean;
}

export default function TaskNodeCard(props: Props) {
  const { node, x, y, width, height, collapsed, isAnchor, dim, sourceSelected, zoomed, zoomComp, childrenCount, inWindow } = props;
  const meta = STATUS_META[node.status];
  const approval = useGraphStore((s) => s.approvalsByNode[node.nodeId]);
  const clickTimer = useRef<number | null>(null);
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState(node.title);
  const [draft, setDraft] = useState('');
  const chatRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const dragRef = useRef<{ sx: number; sy: number; moved: boolean; anchor: 'title' | 'card' } | null>(null);
  const resizeRef = useRef<{ sx: number; sy: number; w: number; h: number } | null>(null);

  const closeMenu = () => setMenu(null);

  const commitTitle = () => {
    if (titleDraft.trim()) useGraphStore.getState().updateNodeTitle(node.nodeId, titleDraft);
    setEditingTitle(false);
  };

  const sendChat = () => {
    const text = draft.trim();
    if (!text) return;
    useGraphStore.getState().sendMessage(node.nodeId, text);
    setDraft('');
    if (inputRef.current) {
      inputRef.current.style.height = 'auto';
    }
  };

  // 多模态：把图片 OCR 成文本，追加到输入框（模型是文本模型，通过 OCR 看图片文字）
  const [ocrBusy, setOcrBusy] = useState(false);
  const handleOcr = async (file: File | Blob, filename?: string) => {
    if (ocrBusy) return;
    setOcrBusy(true);
    try {
      const { text } = await api.ocr(file, filename);
      const piece = text.trim()
        ? `【图片内容】\n${text.trim()}`
        : '【图片内容】(未能识别出文字)';
      setDraft((prev) => (prev ? prev + '\n' + piece : piece));
    } catch {
      alert('图片识别（OCR）失败，请重试');
    } finally {
      setOcrBusy(false);
    }
  };

  // 粘贴图片：拦截并 OCR
  const onChatPaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(e.clipboardData?.files ?? []);
    const img = files.find((f) => f.type.startsWith('image/'));
    if (img) {
      e.preventDefault();
      handleOcr(img, img.name || 'pasted.png');
    }
  };

  // 展开态右下角手柄：拖动调整宽高（按下拖动、松开停止）
  const onResizePointerDown = (e: React.PointerEvent) => {
    e.stopPropagation();
    // 不 preventDefault，避免阻断 pointerup 派发
    const w = node.customSize?.width ?? width;
    const h = node.customSize?.height ?? height;
    resizeRef.current = { sx: e.clientX, sy: e.clientY, w, h };
    const finish = () => {
      if (!resizeRef.current) return;
      resizeRef.current = null;
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', finish);
      window.removeEventListener('pointercancel', finish);
      // 拖动结束持久化最终尺寸
      const finalNode = useGraphStore.getState().nodes[node.nodeId];
      if (finalNode?.customSize) {
        api.updateNode(node.nodeId, { customSize: finalNode.customSize }).catch(() => {});
      }
    };
    const move = (ev: PointerEvent) => {
      const r = resizeRef.current;
      if (!r) return;
      const nw = r.w + (ev.clientX - r.sx);
      const nh = r.h + (ev.clientY - r.sy);
      useGraphStore.getState().resizeNode(node.nodeId, nw, nh);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', finish);
    window.addEventListener('pointercancel', finish);
  };

  // 展开态右下角手柄：拖动调整宽高（已在 onResizePointerDown 内用原生监听处理）

  // 新消息时滚动到底部
  useEffect(() => {
    chatRef.current?.scrollTo({ top: chatRef.current.scrollHeight });
  }, [node.messages?.length]);

  // 展开态：卡片内滚轮直接滚动消息列表，阻止冒泡到画布缩放
  useEffect(() => {
    const el = cardRef.current;
    if (!el || collapsed) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      e.stopPropagation();
      // 鼠标落在内置滚动区（执行轨迹 / 执行过程 / 任务进度 / 审批参数）时，滚动它自身，不滚聊天区
      const t = e.target as HTMLElement | null;
      const scroller = t && t.closest ? t.closest('.exec-fold-body, .exec-body, .tl-body, .card-approval-args') : null;
      if (scroller) {
        scroller.scrollTop += e.deltaY;
        return;
      }
      const chat = chatRef.current;
      if (chat) chat.scrollTop += e.deltaY;
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, [collapsed]);

  useEffect(() => {
    if (!menu) return;
    // 用 mousedown 关闭（点击菜单外），避免 document click 与 React onClick 竞态导致按钮 action 不执行
    const onDocMouseDown = (e: MouseEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && el.closest('.ctx-menu')) return; // 点在菜单内部不关
      setMenu(null);
    };
    document.addEventListener('mousedown', onDocMouseDown);
    return () => document.removeEventListener('mousedown', onDocMouseDown);
  }, [menu]);

  const handleContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    // 用视口坐标 + Portal 渲染到 body，避免被卡片/画布裁剪，也不受 transform 缩放影响
    setMenu({ x: e.clientX, y: e.clientY });
  };

  // 单击=设锚点（延迟以区分双击）；双击=展开/收起（§5.2 交互决议）
  // 选源模式下：点击立即切换选中，取消延迟，反馈即时
  const handleClick = () => {
    if (inWindow) return; // 窗口内单击不设 DAG 锚点
    if (useGraphStore.getState().selectingSource) {
      useGraphStore.getState().clickNode(node.nodeId);
      return;
    }
    if (clickTimer.current != null) window.clearTimeout(clickTimer.current);
    clickTimer.current = window.setTimeout(() => {
      useGraphStore.getState().clickNode(node.nodeId);
    }, 240);
  };

  const handleDoubleClick = () => {
    if (clickTimer.current != null) {
      window.clearTimeout(clickTimer.current);
      clickTimer.current = null;
    }
    // 双击开/关窗口（多窗口）：打开时铺满画布可视区（恢复之前大小）
    const canvasEl = document.querySelector('.canvas') as HTMLElement | null;
    const cw = canvasEl?.clientWidth ?? window.innerWidth;
    const ch = canvasEl?.clientHeight ?? window.innerHeight;
    const rect = {
      x: 24,
      y: 24,
      width: Math.max(320, cw - 48),
      height: Math.max(300, ch - 48),
    };
    useGraphStore.getState().toggleWindow(node.nodeId, rect);
  };

  // 拖动：按住卡片（标题栏优先）横向拖拽，移动超过阈值后整卡跟随
  const onPointerDown = (e: React.PointerEvent) => {
    if (inWindow) return; // 窗口内卡片由窗口标题栏统一拖动，不启用卡片自身拖动
    if (e.button !== 0) return;
    if ((e.target as HTMLElement).closest('button, input, textarea, [contenteditable]')) return;
    const inTitle = !!(e.target as HTMLElement).closest('.card-head');
    dragRef.current = { sx: e.clientX, sy: e.clientY, moved: false, anchor: inTitle ? 'title' : 'card' };
    (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const d = dragRef.current;
    if (!d) return;
    const dx = e.clientX - d.sx;
    const dy = e.clientY - d.sy;
    if (!d.moved && Math.abs(dx) + Math.abs(dy) > 3) d.moved = true;
    if (d.moved) {
      useGraphStore.getState().moveNode(node.nodeId, dx, dy);
      d.sx = e.clientX;
      d.sy = e.clientY;
    }
  };

  const onPointerUp = () => {
    dragRef.current = null;
  };

  const cls = [
    'dag-node',
    'dag-node--task',
    `status-${node.status}`,
    collapsed ? 'is-collapsed' : 'is-expanded',
    isAnchor ? 'is-anchor' : '',
    sourceSelected ? 'is-source-selected' : '',
    zoomed ? 'is-zoomed' : '',
    dim ? 'dimmed' : '',
  ]
    .filter(Boolean)
    .join(' ');

  const prog = node.progress;
  const fmtTok = (n?: number) => {
    const v = n || 0;
    return v >= 1000 ? `${(v / 1000).toFixed(1)}k` : `${v}`;
  };
  const fmtDur = (ms?: number) => {
    const s = Math.max(0, Math.floor((ms || 0) / 1000));
    if (s < 60) return `${s}s`;
    const mm = Math.floor(s / 60);
    const ss = s % 60;
    return `${mm}m${ss}s`;
  };
  // 进度：优先用计划完成度（已完成步骤/总步骤），否则退回按耗时估算
  const stepTotal = prog?.stepTotal ?? 0;
  const stepDone = prog?.stepDone ?? 0;
  const stepPct =
    stepTotal > 0 ? Math.min(100, Math.round((stepDone / stepTotal) * 100)) : -1;
  const pct =
    stepPct >= 0
      ? stepPct
      : prog && prog.expectedMs > 0
        ? Math.min(100, Math.round((prog.elapsedMs / prog.expectedMs) * 100))
        : 0;

  // 可删除：无下游依赖即可删（有下游暂不允许）
  const canDelete = childrenCount === 0;

  const confirmDelete = () => {
    if (window.confirm(`确定删除卡片「${node.title}」吗？`)) {
      // 先执行删除，再关闭菜单，保证 store 状态一致
      useGraphStore.getState().removeNode(node.nodeId);
      closeMenu();
    } else {
      closeMenu();
    }
  };

  return (
    <div
      ref={cardRef}
      className={cls}
      style={{ left: x, top: y, width, height, '--zoom-comp': zoomComp ?? 0.5 } as React.CSSProperties}
      onClick={handleClick}
      onDoubleClick={handleDoubleClick}
      onContextMenu={handleContextMenu}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
    >
      {menu
        ? createPortal(
            <div className="ctx-menu" style={{ position: 'fixed', left: menu.x, top: menu.y }} onClick={(e) => e.stopPropagation()}>
          <button
            className="ctx-item"
            onMouseDown={(e) => {
              e.preventDefault();
              e.stopPropagation();
              useGraphStore.getState().clickNode(node.nodeId);
              closeMenu();
            }}
          >
            设为锚点
          </button>
          {['running', 'ready', 'pending'].includes(node.status) ? (
            <button
              className="ctx-item"
              onMouseDown={(e) => {
                e.preventDefault();
                e.stopPropagation();
                useGraphStore.getState().cancelNode(node.nodeId);
                closeMenu();
              }}
            >
              取消执行
            </button>
          ) : null}
          {['failed', 'cancelled'].includes(node.status) ? (
            <button
              className="ctx-item"
              onMouseDown={(e) => {
                e.preventDefault();
                e.stopPropagation();
                useGraphStore.getState().retryNode(node.nodeId);
                closeMenu();
              }}
            >
              重试
            </button>
          ) : null}
          <button
            className="ctx-item"
            onMouseDown={(e) => {
              e.preventDefault();
              e.stopPropagation();
              useGraphStore.getState().toggleCollapsed(node.nodeId);
              closeMenu();
            }}
          >
            {collapsed ? '展开' : '收起'}
          </button>
          <div className="ctx-sep" />
          <div className="ctx-group-label">新建卡片</div>
          <button
            className="ctx-item"
            onMouseDown={(e) => {
              e.preventDefault();
              e.stopPropagation();
              useGraphStore.getState().quickCreate(node.nodeId, 'serial');
              closeMenu();
            }}
          >
            ＋ 串行（下游）
          </button>
          <button
            className="ctx-item"
            onMouseDown={(e) => {
              e.preventDefault();
              e.stopPropagation();
              useGraphStore.getState().quickCreate(node.nodeId, 'parallel');
              closeMenu();
            }}
          >
            ＋ 并行（同层）
          </button>
          <button
            className="ctx-item"
            onMouseDown={(e) => {
              e.preventDefault();
              e.stopPropagation();
              useGraphStore.getState().quickCreate(node.nodeId, 'join');
              closeMenu();
            }}
          >
            ＋ 合并（join）
          </button>
          {canDelete ? (
            <button
              className="ctx-item ctx-item-danger"
              onMouseDown={(e) => {
                e.preventDefault();
                e.stopPropagation();
                confirmDelete();
              }}
            >
              删除卡片
            </button>
          ) : null}
            </div>,
            document.body,
          )
        : null}
      <div className="card-head">
        <span className="status-dot" />
        {editingTitle ? (
          <input
            className="title-edit-input"
            autoFocus
            value={titleDraft}
            onChange={(e) => setTitleDraft(e.target.value)}
            onBlur={commitTitle}
            onKeyDown={(e) => {
              if (e.key === 'Enter') commitTitle();
              if (e.key === 'Escape') setEditingTitle(false);
            }}
            onClick={(e) => e.stopPropagation()}
            onPointerDown={(e) => e.stopPropagation()}
          />
        ) : (
          <div className="card-title" title={`${node.title}（点 ✎ 编辑标题）`}>
            {node.title}
          </div>
        )}
        <div className="card-head-actions">
          <span className="status-badge">{meta.label}</span>
          {sourceSelected ? <span className="source-badge">✓ 已选</span> : null}
          <button
            className="icon-btn"
            title="编辑标题"
            onClick={(e) => {
              e.stopPropagation();
              setTitleDraft(node.title);
              setEditingTitle(true);
            }}
          >
            ✎
          </button>
          <button
            className="icon-btn"
            title={collapsed ? '展开' : '收起'}
            onClick={(e) => {
              e.stopPropagation();
              useGraphStore.getState().toggleCollapsed(node.nodeId);
            }}
          >
            {collapsed ? '▾' : '▴'}
          </button>
          <button
            className="icon-btn anchor-btn"
            title="设为锚点"
            onClick={(e) => {
              e.stopPropagation();
              useGraphStore.getState().clickNode(node.nodeId);
            }}
          >
            ⌖
          </button>
          {canDelete ? (
            <button
              className="icon-btn delete-btn"
              title="删除卡片"
              onClick={(e) => {
                e.stopPropagation();
                confirmDelete();
              }}
            >
              ✕
            </button>
          ) : null}
        </div>
      </div>

      {collapsed ? (
        <div className="card-body-collapsed">
          <div className="card-summary">
            {node.output?.summary ??
              (node.status === 'pending'
                ? '等待上游节点完成…'
                : node.status === 'ready'
                  ? '就绪，等待调度…'
                  : node.status === 'running'
                    ? '执行中…'
                    : node.status === 'paused'
                      ? '已暂停，等待恢复'
                      : node.status === 'blocked'
                        ? '等待用户裁决（跳过失败 / 取消）'
                        : node.status === 'done'
                          ? '已完成，双击查看详情'
                          : node.meta.failedReason ?? '')}
          </div>
          {(node.status === 'running' || node.status === 'paused') && prog ? (
            <>
              <div className="card-progress">
                <div className="card-progress-inner" style={{ width: `${pct}%` }} />
              </div>
              <div className="card-meta-line">
                {stepTotal > 0 ? (
                  <span className="step-indicator">第 {Math.min(stepDone + 1, stepTotal)}/{stepTotal} 步 · {pct}%</span>
                ) : (
                  <span>{pct}%</span>
                )}
                <span>{fmtTok(prog?.tokens)} tok</span>
                <span>{fmtDur(prog?.elapsedMs)}</span>
              </div>
            </>
          ) : null}
          <div className="card-meta">
            {node.meta.mode ? <span className="mode-chip">{MODE_LABEL[node.meta.mode]}</span> : null}
            {node.subtreeCollapsed ? <span className="mode-chip fold-chip">子树已折叠</span> : null}
            <span>{childrenCount} 个子任务</span>
            {prog?.elapsedMs != null && node.status !== 'running' && node.status !== 'paused' ? (
              <span>{fmtDur(prog?.elapsedMs)}</span>
            ) : null}
          </div>
          <QuickCreateButtons nodeId={node.nodeId} />
        </div>
      ) : (
        <div className="card-body-expanded">
          <ParentContextCard parentContext={node.parentContext ?? []} />
          <ExecTimeline nodeId={node.nodeId} running={node.status === 'running'} refreshKey={node.updatedAt ?? 0} />
          <div className="chat-flow" ref={chatRef}>
            {(() => {
              // 对齐 dsh：按"每条 user 提问 = 一组"分组。
              // - 同一轮里所有 assistant 回复合并到一个框（markdown 层级结构）
              // - 该轮的执行步骤整合到一个"执行过程"折叠块，与消息框接壤
              const groups: { user: any; replies: any[]; steps: any[] }[] = [];
              for (const m of node.messages ?? []) {
                if (m.role === 'user') {
                  groups.push({ user: m, replies: [], steps: [] });
                } else if (m.role === 'assistant') {
                  if (groups.length) groups[groups.length - 1].replies.push(m);
                  else groups.push({ user: null as any, replies: [m], steps: [] });
                } else {
                  // system 步骤 → 挂到当前组（执行过程）
                  if (groups.length) groups[groups.length - 1].steps.push(m);
                }
              }
              if (groups.length === 0) {
                return <div className="chat-empty">在这个卡片里开始对话吧，可连续多轮追问。</div>;
              }
              return (
                <>
                  {groups.map((g, gi) => (
                    <div key={g.user?.id ?? `g${gi}`} className="chat-group">
                      {/* 用户提问 */}
                      {g.user ? (
                        <div className="chat-msg chat-user">
                          <div className="chat-bubble-wrap">
                            <div className="chat-bubble">{g.user.text}</div>
                            {/* 用户消息的一键复制：放在消息框外面 */}
                            <button
                              className="copy-btn"
                              title="复制"
                              onClick={(e) => {
                                e.stopPropagation();
                                if (g.user.text) navigator.clipboard?.writeText(g.user.text);
                              }}
                            >
                              ⧉
                            </button>
                          </div>
                        </div>
                      ) : null}
                      {/* 同一轮的所有 assistant 回复 → 合并一个框（markdown 层级），一键复制在右下角 */}
                      {g.replies.length > 0 ? (
                        <div className="chat-msg chat-assistant">
                          <div className="chat-bubble-wrap">
                            <div className="chat-bubble">
                              {/* 仅当前正在回复的气泡显示执行步骤；已回复的气泡保持最终内容 */}
                              {node.status === 'running' && gi === groups.length - 1 && g.replies.some((rr) => rr.streaming) ? (
                                <ExecLivePreview nodeId={node.nodeId} running />
                              ) : null}
                              {g.replies.map((r, ri) => (
                                <div key={r.id ?? ri} className="assistant-reply">
                                  <MarkdownBlock text={r.text} />
                                  {r.streaming ? <span className="chat-cursor">▍</span> : null}
                                </div>
                              ))}
                              <button
                                className="copy-btn"
                                title="复制"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  const all = g.replies.map((r) => r.text).join('\n');
                                  if (all) navigator.clipboard?.writeText(all);
                                }}
                              >
                                ⧉
                              </button>
                            </div>
                          </div>
                        </div>
                      ) : null}
                    </div>
                  ))}
                </>
              );
            })()}
          </div>

          {node.status === 'failed' && node.meta.failedReason ? (
            <div className="fail-reason">{node.meta.failedReason}</div>
          ) : null}

          <div className="chat-input-box">
            {/* 任务进度栏：融入输入栏，样式与输入栏一致 */}
            <ExecutionCard plan={node.plan} running={node.status === 'running'} />
            <div className="chat-input-top">
              <textarea
                ref={inputRef}
                className="chat-input"
                value={draft}
                onChange={(e) => {
                  setDraft(e.target.value);
                  // DeepSeek 风格：内容多时自适应长高，上限约 140px
                  const el = e.target;
                  el.style.height = 'auto';
                  el.style.height = `${Math.min(140, el.scrollHeight)}px`;
                }}
                onClick={(e) => e.stopPropagation()}
                onPointerDown={(e) => e.stopPropagation()}
                onPaste={onChatPaste}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    sendChat();
                  }
                }}
                placeholder={node.status === 'running' ? '正在回复…' : '输入新一轮问题（Enter 发送）'}
                rows={3}
              />
              {node.status === 'running' || node.status === 'paused' ? (
                <button
                  className="btn btn-warn chat-send-btn"
                  onClick={(e) => {
                    e.stopPropagation();
                    useGraphStore.getState().cancelNode(node.nodeId);
                  }}
                >
                  停止
                </button>
              ) : (
                <button
                  className="btn btn-primary chat-send-btn"
                  disabled={!draft.trim()}
                  onClick={(e) => {
                    e.stopPropagation();
                    sendChat();
                  }}
                >
                  发送
                </button>
              )}
            </div>
            <div className="chat-input-toolbar">
              <label className="upload-btn" title="上传文件到工作区">
                ＋
                <input
                  type="file"
                  hidden
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) useGraphStore.getState().uploadNodeFile(node.nodeId, f);
                    e.target.value = '';
                  }}
                />
              </label>
              <label className="upload-btn" title="图片：上传并识别图中文字（OCR）" style={{ cursor: ocrBusy ? 'wait' : 'pointer' }}>
                {ocrBusy ? '…' : '🖼'}
                <input
                  type="file"
                  accept="image/*"
                  hidden
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) handleOcr(f, f.name);
                    e.target.value = '';
                  }}
                />
              </label>
              <select
                className="model-select"
                value={node.model || ''}
                onChange={(e) => useGraphStore.getState().setNodeModel(node.nodeId, e.target.value)}
                onClick={(e) => e.stopPropagation()}
                onPointerDown={(e) => e.stopPropagation()}
                title="选择模型"
              >
                <option value="">默认</option>
                {useGraphStore.getState().availableModels.map((m) => (
                  <option key={`${m.provider}-${m.model}`} value={m.model}>
                    {m.model}
                  </option>
                ))}
              </select>
              <TrustLevelPicker />
            </div>
          </div>

          {approval ? (
            <div className="card-approval" onClick={(e) => e.stopPropagation()}>
              <div className="card-approval-title">⚠ 工具待审批</div>
              <div className="card-approval-tool">{approval.tool}</div>
              {approval.args ? (
                <pre className="card-approval-args">
                  {Object.entries(approval.args)
                    .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
                    .join('\n')}
                </pre>
              ) : null}
              <div className="card-approval-actions">
                <button
                  className="btn btn-primary"
                  onClick={(e) => {
                    e.stopPropagation();
                    useGraphStore.getState().approveApproval(approval.approvalId, node.nodeId);
                  }}
                >
                  允许
                </button>
                <button
                  className="btn btn-ghost"
                  onClick={(e) => {
                    e.stopPropagation();
                    useGraphStore.getState().rejectApproval(approval.approvalId, node.nodeId);
                  }}
                >
                  拒绝
                </button>
              </div>
            </div>
          ) : null}

          <div className="card-actions">
            {node.status === 'blocked' ? (
              <>
                <button
                  className="btn btn-warn"
                  onClick={(e) => {
                    e.stopPropagation();
                    useGraphStore.getState().resolveBlocked(node.nodeId, 'skip');
                  }}
                >
                  跳过失败继续
                </button>
                <button
                  className="btn btn-ghost"
                  onClick={(e) => {
                    e.stopPropagation();
                    useGraphStore.getState().resolveBlocked(node.nodeId, 'cancel');
                  }}
                >
                  整体取消
                </button>
              </>
            ) : null}
            {['running', 'ready', 'pending'].includes(node.status) ? (
              <button
                className="btn btn-ghost"
                onClick={(e) => {
                  e.stopPropagation();
                  useGraphStore.getState().cancelNode(node.nodeId);
                }}
              >
                取消
              </button>
            ) : null}
            {node.status === 'running' ? (
              <button
                className="btn btn-ghost"
                onClick={(e) => {
                  e.stopPropagation();
                  useGraphStore.getState().pauseNode(node.nodeId);
                }}
              >
                暂停
              </button>
            ) : null}
            {node.status === 'paused' ? (
              <button
                className="btn btn-ghost"
                onClick={(e) => {
                  e.stopPropagation();
                  useGraphStore.getState().resumeNode(node.nodeId);
                }}
              >
                恢复
              </button>
            ) : null}
            {['failed', 'cancelled'].includes(node.status) ? (
              <button
                className="btn btn-ghost"
                onClick={(e) => {
                  e.stopPropagation();
                  useGraphStore.getState().retryNode(node.nodeId);
                }}
              >
                重试
              </button>
            ) : null}
            {childrenCount > 0 ? (
              <button
                className="btn btn-ghost"
                onClick={(e) => {
                  e.stopPropagation();
                  useGraphStore.getState().toggleSubtree(node.nodeId);
                }}
              >
                {node.subtreeCollapsed ? '展开子树' : '折叠子树'}
              </button>
            ) : null}
          </div>
          <QuickCreateButtons nodeId={node.nodeId} compact />
          {!inWindow ? (
            <div
              className="resize-handle"
              title="拖动调整卡片大小"
              onPointerDown={onResizePointerDown}
            >
              ◢
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

function ExecutionCard({ plan, running }: { plan?: { goal?: string; steps?: { label: string; status: string }[] }; running?: boolean }) {
  const planSteps = plan?.steps ?? [];
  if (planSteps.length === 0) return null;
  const [expanded, setExpanded] = useState(false);
  // 运行时自动展开，让计划步骤状态实时可见；结束后保持展开（可手动收起）
  useEffect(() => {
    if (running) setExpanded(true);
  }, [running]);
  const doneCount = planSteps.filter((s) => s.status === 'done').length;
  const goal = plan?.goal || '任务执行';
  return (
    <div className="exec-card">
      <button
        className="exec-head"
        onClick={(e) => {
          e.stopPropagation();
          setExpanded(!expanded);
        }}
      >
        <span className="exec-icon">🧭</span>
        <span className="exec-goal">{goal}</span>
        <span className="exec-step-count">第 {Math.min(doneCount + 1, planSteps.length)}/{planSteps.length} 步 · {Math.round((doneCount / planSteps.length) * 100)}%</span>
        <span className="exec-chevron">{expanded ? '▴' : '▾'}</span>
      </button>
      {expanded ? (
        <div className="exec-body">
          {planSteps.map((s, i) => (
            <div key={`p-${i}`} className={`exec-item exec-plan-${s.status}`}>
              <span className="exec-item-icon">{statusPlanIcon(s.status)}</span>
              <span className="exec-item-label">{s.label}</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function statusPlanIcon(s: string): string {
  return s === 'done' ? '✓' : s === 'running' ? '⏳' : s === 'failed' ? '✗' : '○';
}

function ParentContextCard({ parentContext }: { parentContext: { parentNodeId: string; parentTitle: string; seq?: number; summary: string }[] }) {
  if (!parentContext || parentContext.length === 0) return null;
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="parent-context-card">
      <button className="parent-context-head" onClick={() => setExpanded(!expanded)}>
        <span className="parent-context-label">上游上下文</span>
        <span className="parent-context-count">{parentContext.length} 块</span>
        <span className="parent-context-chevron">{expanded ? '▴' : '▾'}</span>
      </button>
      {expanded ? (
        <div className="parent-context-body">
          {parentContext.map((b, i) => (
            <div key={`${b.parentNodeId}-${b.seq ?? i}`} className="parent-context-item">
              <span className="parent-context-title">{b.parentTitle}{b.seq ? ` #${b.seq}` : ''}</span>
              <span className="parent-context-summary">{b.summary}</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function QuickCreateButtons({ nodeId, compact }: { nodeId: string; compact?: boolean }) {
  return (
    <div className={`card-append-quick${compact ? ' is-compact' : ''}`}>
      <button
        className="quick-btn"
        title="创建串行子卡片（引用本卡片内容）"
        onClick={(e) => {
          e.stopPropagation();
          useGraphStore.getState().quickCreate(nodeId, 'serial');
        }}
      >
        ＋ 串行
      </button>
      <button
        className="quick-btn"
        title="创建并行兄弟卡片"
        onClick={(e) => {
          e.stopPropagation();
          useGraphStore.getState().quickCreate(nodeId, 'parallel');
        }}
      >
        ＋ 并行
      </button>
      <button
        className="quick-btn"
        title="合并本卡片与其它卡片（进入选源）"
        onClick={(e) => {
          e.stopPropagation();
          useGraphStore.getState().quickCreate(nodeId, 'join');
        }}
      >
        ＋ 合并
      </button>
    </div>
  );
}

