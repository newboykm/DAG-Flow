/**
 * ExecLivePreview — 大模型执行时的实时过程预览。
 * 在执行(running)期间，从 event_log 的最新 run 里取“最近动作”，显示在回复气泡内，
 * 避免气泡长时间空白；执行完成(running=false)后该组件不渲染，气泡只剩最终输出。
 */
import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { onExecEvent } from '../store/useGraphStore';

interface Ev { kind: string; ts?: number; [k: string]: unknown; }

export default function ExecLivePreview({ nodeId, running }: { nodeId: string; running: boolean }) {
  const [last, setLast] = useState<string>('正在执行…');
  const timer = useRef<number | null>(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const r = await api.getNodeEvents(nodeId);
        if (!alive || !r?.events || r.events.length === 0) return;
        // 取最新 run 的最后几个动作，转成短文案
        const evs = r.events as Ev[];
        let runs: Ev[][] = [];
        let cur: Ev[] = [];
        for (const e of evs) {
          if (e.kind === 'turn_start' && cur.length) { runs.push(cur); cur = []; }
          cur.push(e);
        }
        if (cur.length) runs.push(cur);
        const latest = runs[runs.length - 1] ?? cur;
        const actions: string[] = [];
        for (const e of latest.slice(-6)) {
          if (e.kind === 'tool_call') actions.push(`调用 ${String(e.tool ?? '')}`);
          else if (e.kind === 'tool_result') actions.push(e.ok === false ? `✗ ${String(e.tool ?? '')} 失败` : `✓ ${String(e.tool ?? '')} 完成`);
          else if (e.kind === 'llm_done') { const t = String(e.text ?? '').slice(0, 40); if (t) actions.push(`💬 ${t}`); }
          else if (e.kind === 'step_start') actions.push(`第 ${String(e.round ?? 0)} 步`);
        }
        if (actions.length) {
          const tail = actions.slice(-3).join(' · ');
          setLast(`⏳ ${tail ?? '执行中…'}`);
        }
      } catch { /* ignore */ }
    };
    if (running) {
      load();
      timer.current = window.setInterval(load, 2000);
    }
    // 订阅 WS 实时 exec_event，动作即时更新
    const unsub = onExecEvent((e) => {
      if (e && (e.nodeId === nodeId || e.nodeId === undefined)) load();
    });
    return () => { alive = false; unsub(); if (timer.current) window.clearInterval(timer.current); };
  }, [nodeId, running]);

  if (!running) return null;
  return <div className="exec-live"><span className="exec-live-dot" />{last}</div>;
}
