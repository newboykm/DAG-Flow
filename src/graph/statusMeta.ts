import type { NodeStatus } from '../types';

export interface StatusMeta {
  label: string;
  color: string;
  bg: string;
  border: string;
}

export const STATUS_META: Record<NodeStatus, StatusMeta> = {
  pending: { label: '等待', color: '#64748b', bg: '#f1f5f9', border: '#cbd5e1' },
  ready: { label: '就绪', color: '#0e7490', bg: '#ecfeff', border: '#67e8f9' },
  running: { label: '执行中', color: '#1d4ed8', bg: '#eff6ff', border: '#93c5fd' },
  done: { label: '完成', color: '#15803d', bg: '#f0fdf4', border: '#86efac' },
  failed: { label: '失败', color: '#b91c1c', bg: '#fef2f2', border: '#fca5a5' },
  blocked: { label: '阻塞', color: '#b45309', bg: '#fffbeb', border: '#fcd34d' },
  cancelled: { label: '已取消', color: '#475569', bg: '#f8fafc', border: '#cbd5e1' },
  paused: { label: '已暂停', color: '#7c3aed', bg: '#f5f3ff', border: '#c4b5fd' },
};
