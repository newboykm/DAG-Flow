import type { DagNode } from '../types';
import { makeTask, newRoot, type MakeTaskOpts } from './nodeFactory';

export interface SessionData {
  nodes: Record<string, DagNode>;
  rootId: string;
}

/** 小型演示：覆盖串行/并行/join、以及「失败父 → join 下游 blocked」场景。 */
export function buildDemo(): SessionData {
  const nodes: Record<string, DagNode> = {};
  const root = newRoot('demo');
  nodes[root.nodeId] = root;

  const add = (t: Omit<MakeTaskOpts, 'sessionId'>) => {
    const n = makeTask({ ...t, sessionId: 'demo' });
    nodes[n.nodeId] = n;
    return n.nodeId;
  };

  const A = add({ title: '分析目标代码结构', parentIds: ['root'], status: 'done', mode: 'serial' });
  const B = add({ title: '检索同类问题的解决方案', parentIds: ['root'], status: 'done', mode: 'parallel' });
  const C = add({ title: '整理代码依赖关系', parentIds: [A], status: 'done', mode: 'serial' });
  add({ title: '综合结果撰写重构方案', parentIds: [C, B], status: 'ready', mode: 'join' });

  const F = add({
    title: '执行回归测试（模拟失败）',
    parentIds: ['root'],
    status: 'failed',
    mode: 'parallel',
    failedReason: '超时（10 分钟）',
  });
  const G = add({ title: '收集性能基准数据', parentIds: ['root'], status: 'done', mode: 'parallel' });
  add({ title: '汇总测试与性能结论', parentIds: [F, G], status: 'blocked', mode: 'join' });

  return { nodes, rootId: root.nodeId };
}

const TITLES = [
  '数据清洗', '特征提取', '模型训练', '参数调优', '交叉验证', '结果分析',
  '代码审查', '单元测试', '性能基准', '文档整理', '依赖升级', '接口联调',
];

/** 大图演示：分层生成一棵多叉树（默认 130 节点），并附加若干 join 与失败叶子。 */
export function buildLarge(target = 130): SessionData {
  const nodes: Record<string, DagNode> = {};
  const root = newRoot('large');
  nodes[root.nodeId] = root;

  const childrenOf: Record<string, string[]> = {};
  let created = 0;
  let frontier: string[] = ['root'];
  let ti = 0;

  while (created < target && frontier.length > 0) {
    const next: string[] = [];
    for (const pid of frontier) {
      if (created >= target) break;
      const cnt = Math.random() < 0.3 ? 3 : 2;
      for (let i = 0; i < cnt && created < target; i++) {
        created += 1;
        const title = `${TITLES[ti % TITLES.length]} · 任务${created}`;
        ti += 1;
        const n = makeTask({
          title,
          parentIds: [pid],
          sessionId: 'large',
          status: 'done',
          mode: i === 0 ? 'serial' : 'parallel',
        });
        nodes[n.nodeId] = n;
        (childrenOf[pid] ??= []).push(n.nodeId);
        next.push(n.nodeId);
      }
    }
    frontier = next;
  }

  // 附加 join：把若干兄弟节点对合并成一个共同下游
  const joinIds: string[] = [];
  let joins = 0;
  for (const kids of Object.values(childrenOf)) {
    if (kids.length >= 2 && joins < 5) {
      const j = makeTask({
        title: `合并结果 · 汇总${joins + 1}`,
        parentIds: [kids[0], kids[1]],
        sessionId: 'large',
        status: 'ready',
        mode: 'join',
      });
      nodes[j.nodeId] = j;
      joinIds.push(j.nodeId);
      joins += 1;
    }
  }

  // 若干叶子节点置为失败（叶子无下游，不产生不一致）
  const taskIds = Object.keys(nodes).filter((id) => nodes[id].kind === 'task');
  const leaves = taskIds.filter((id) => !(childrenOf[id]?.length) && !joinIds.includes(id));
  let failed = 0;
  for (const id of leaves) {
    if (failed >= 3) break;
    nodes[id] = {
      ...nodes[id],
      status: 'failed',
      meta: { ...nodes[id].meta, failedReason: '超时（10 分钟）' },
    };
    failed += 1;
  }

  return { nodes, rootId: root.nodeId };
}
