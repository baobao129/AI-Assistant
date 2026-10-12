/**
 * 单个 Run 的 SSE 客户端（API-005 / ADR-010）。
 *
 * - 断线后使用 `Last-Event-ID` 续传回放保留窗口内的事件；
 * - 按 `event_id` 去重，服务端回放或网络层重复投递都不会重复触发客户端副作用；
 * - 收到终态 `final` 或终止 `error` 后关闭流，不再重连；
 * - 重连永远沿用同一个 Run，绝不创建新的 Run。
 */

import { authHeaders } from "./api.js";
import { errorMessage } from "./labels.js";

export class RunStream {
  constructor(runId, { onEvent, onError, onClose, lastEventId = null } = {}) {
    this.runId = runId;
    this.onEvent = onEvent || (() => {});
    this.onError = onError || (() => {});
    this.onClose = onClose || (() => {});
    this.lastEventId = lastEventId;
    this.seen = new Set();
    this.controller = null;
    this.closed = false;
    // 初始重连延迟 500ms，网络类失败时按指数退避倍增（上限 8000ms）。
    this.reconnectDelay = 500;
  }

  async start() {
    this.closed = false;
    await this.connect();
  }

  async connect() {
    if (this.closed) return;
    this.controller = new AbortController();
    try {
      const headers = authHeaders({ Accept: "text/event-stream" });
      if (this.lastEventId) headers["Last-Event-ID"] = this.lastEventId;
      const response = await fetch(`/api/v1/runs/${this.runId}/events`, {
        headers,
        signal: this.controller.signal,
      });
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        const error = new Error(
          errorMessage(body?.error, '连接已中断，请刷新对话查看结果。'),
        );
        error.status = response.status;
        throw error;
      }
      await this.read(response);
    } catch (error) {
      if (this.closed || error.name === "AbortError") return;
      // 回放窗口过期（409）、Run 不存在（404）或凭据失效（401）属于确定性失败，
      // 重连无法恢复，因此只上报一次并立即关闭，绝不进入无限重连。
      if (
        error.status === 409 ||
        error.status === 404 ||
        error.status === 401
      ) {
        this.onError(error);
        this.onClose();
        return;
      }
      this.onError(error);
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, 8000);
      setTimeout(() => this.connect(), this.reconnectDelay);
    }
  }

  async read(response) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let terminal = false;
    while (!this.closed) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let separator = buffer.indexOf("\n\n");
      while (separator !== -1) {
        const chunk = buffer.slice(0, separator);
        buffer = buffer.slice(separator + 2);
        const event = this.parseChunk(chunk);
        if (event) {
          terminal =
            event.event_type === "final" || this.isTerminalError(event);
          if (!this.seen.has(event.event_id)) {
            this.seen.add(event.event_id);
            this.lastEventId = event.event_id;
            this.onEvent(event);
          }
          if (terminal) {
            this.close();
            this.onClose();
            return;
          }
        }
        separator = buffer.indexOf("\n\n");
      }
    }
    if (!this.closed && !terminal) {
      // 服务端关闭了连接且尚未到达终态：稍后重连，并携带 Last-Event-ID 继续回放。
      setTimeout(() => this.connect(), this.reconnectDelay);
    }
  }

  parseChunk(chunk) {
    let data = null;
    for (const line of chunk.split("\n")) {
      if (line.startsWith("data: ")) {
        try {
          data = JSON.parse(line.slice(6));
        } catch {
          data = null;
        }
      }
    }
    return data;
  }

  isTerminalError(event) {
    return (
      event.event_type === "error" &&
      event.payload &&
      event.payload.terminal === true
    );
  }

  close() {
    this.closed = true;
    this.controller?.abort();
  }
}
