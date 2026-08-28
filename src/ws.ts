// WebSocket 连接管理：断线自动重连 + 重连后补拉差异（文档 §6 WS 断线重连）

export interface WsHandle {
  close: () => void;
  onReconnect?: () => void;
}

/**
 * 建立 WS 连接，断线自动重连（指数退避）。
 * 返回 handle.close() 终止整条连接与重连。
 * handle.onReconnect 在每次成功建立连接后触发（用于补拉差异）。
 */
export function connectWs(
  sessionId: string,
  onEvent: (ev: any) => void,
  onReconnect?: () => void,
): WsHandle {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${proto}://${location.host}/ws/sessions/${sessionId}`;

  let ws: WebSocket | null = null;
  let closed = false;
  let retryTimer: number | null = null;
  let attempts = 0;
  let firstConnect = true;

  const clearRetry = () => {
    if (retryTimer != null) {
      window.clearTimeout(retryTimer);
      retryTimer = null;
    }
  };

  const open = () => {
    if (closed) return;
    ws = new WebSocket(url);

    ws.onopen = () => {
      attempts = 0;
      onReconnect?.();
      if (firstConnect) firstConnect = false;
    };

    ws.onmessage = (e) => {
      try {
        onEvent(JSON.parse(e.data));
      } catch {
        /* ignore */
      }
    };

    ws.onclose = () => {
      if (closed) return;
      // 指数退避重连：1s, 2s, 4s, ... 上限 15s
      attempts += 1;
      const delay = Math.min(15000, 1000 * 2 ** (attempts - 1));
      retryTimer = window.setTimeout(open, delay);
    };

    ws.onerror = () => {
      // onclose 会随后触发，统一在 onclose 处理重连
    };
  };

  open();

  return {
    close: () => {
      closed = true;
      clearRetry();
      try {
        ws?.close();
      } catch {
        /* ignore */
      }
      ws = null;
    },
  };
}
