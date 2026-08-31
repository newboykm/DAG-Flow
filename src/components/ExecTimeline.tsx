/**
 * ExecTimeline — 细粒度执行轨迹（对齐 dsh 的 session event log 可视化）。
 * 从 /api/nodes/{nid}/events 拉取事件，渲染 turn→step→工具→结果 的精简时间线，
 * 与"执行过程"折叠块互补：这里显示 event_log 的机制级细节（工具结果 ok/失败、token 等）。
 */
import { useEffect, useRef, useState } from 'react';
import { api } from '../api';

interface Ev {
  kind: string;
  seq: number;
  [k: string]: unknown;
}

function fmtTime(ts?: number): string {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return `${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`;
}

export default function ExecTimeline({
  nodeId,
  running,
  refreshKey,
}: {
  nodeId: string;
  running: boolean;
  refreshKey?: number;
}) {
  const [events, setEvents] = useState<Ev[]>([]);
  const [open, setOpen] = useState(false);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    let alive = true;
    // 节点/执行切换时先清空旧轨迹，避免显示上一个任务的残留
    setEvents([]);
    if (timer.current) window.clearInterval(timer.current);
    const load = async () => {
      try {
        const r = await api.getNodeEvents(nodeId);
        if (alive && r?.events) setEvents(r.events);
      } catch { /* ignore */ }
    };
    load();
    // 运行时每 2s 刷新，结束后一次性拉全
    if (running) {
      timer.current = window.setInterval(load, 2000);
    }
    return () => {
      alive = false;
      if (timer.current) window.clearInterval(timer.current);
    };
  }, [nodeId, running, refreshKey]);

  if (events.length === 0) return null;

  // 始终显示最新一次执行(run)的轨迹；历史 run 不展示，刷新时替换为最新
  const runs: Ev[][] = [];
  let cur: Ev[] = [];
  for (const e of events) {
    if (e.kind === 'turn_start' && cur.length) { runs.push(cur); cur = []; }
    cur.push(e);
  }
  if (cur.length) runs.push(cur);
  const target = runs[runs.length - 1] ?? [];
  if (target.length === 0) return null;
  const curEvents = target;

  const summary = {
    tools: curEvents.filter((e) => e.kind === 'tool_call').length,
    results: curEvents.filter((e) => e.kind === 'tool_result').length,
    turns: curEvents.filter((e) => e.kind === 'turn_start').length,
  };

  const line = (e: Ev, i: number) => {
    switch (e.kind) {
      case 'turn_start':
        return <div key={i} className="tl-turn">▶ 执行开始</div>;
      case 'step_start':
        return <div key={i} className="tl-step">第 {(e.round as number ?? 0) + 1} 步</div>;
      case 'tool_call':
        return <div key={i} className="tl-call">⚙ {String(e.tool ?? '')}</div>;
      case 'tool_result': {
        const ok = e.ok !== false;
        return (
          <div key={i} className={`tl-result ${ok ? 'ok' : 'bad'}`}>
            {ok ? '✓' : '✗'} {(e.result_preview as string ?? '').slice(0, 60)}
          </div>
        );
      }
      case 'llm_done':
        return <div key={i} className="tl-llm">💬 {String((e.text as string ?? '').slice(0, 40))}</div>;
      case 'turn_end':
        return <div key={i} className="tl-end">■ 完成（{String(e.status ?? '')}）</div>;
      default:
        return null;
    }
  };

  return (
    <details className="tl" open={open} onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary className="tl-head">
        <span>执行轨迹</span>
        <span className="tl-count">{summary.turns}轮 / {summary.tools}工具 / {summary.results}结果</span>
        <span className="tl-chevron">{open ? '▴' : '▾'}</span>
      </summary>
      <div className="tl-body">
        {curEvents.map((e, i) => ({ el: line(e, i), ts: fmtTime(e.ts as number | undefined) }))
          .filter((x) => x.el !== null)
          .map((x, i) => (
            <div key={i} className="tl-row">
              <span className="tl-time">{x.ts}</span>
              <div className="tl-content">{x.el}</div>
            </div>
          ))}
      </div>
    </details>
  );
}
