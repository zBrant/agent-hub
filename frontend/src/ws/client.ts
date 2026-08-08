import {
  type ClientFrame,
  decodeServerFrame,
  type EventFrame,
  encodeClientFrame,
  type NodeStatusFrame,
  type Topic,
  type TopicPayload,
} from "@/ws/protocol";

export type ConnectionStatus =
  | "idle"
  | "connecting"
  | "open"
  | "reconnecting"
  | "closed";

export type TopicHandler = (
  payload: TopicPayload,
  frame: EventFrame | NodeStatusFrame,
) => void;

type TopicCursor = {
  stream: string;
  seq: number;
};

export type WebSocketClientOptions = {
  /** Absolute or same-origin path. Vite proxies `/ws` to 127.0.0.1:8000. */
  url: string;
  onStatus?: (status: ConnectionStatus, attempt: number) => void;
  /** First retry delay in ms. Doubles up to `maxBackoffMs`, with full jitter. */
  baseBackoffMs?: number;
  maxBackoffMs?: number;
};

const DEFAULT_BASE_BACKOFF_MS = 500;
const DEFAULT_MAX_BACKOFF_MS = 15_000;

/**
 * The application's single WebSocket connection (docs/architecture.md §6).
 *
 * One connection multiplexed by topic; one per panel overloads the backend and
 * produces divergent event ordering between components. Framework-agnostic on
 * purpose — React sees it through `WebSocketProvider`, and nothing here imports
 * a store, so live state stays in Zustand and only in Zustand.
 */
export class WebSocketClient {
  readonly #url: string;
  readonly #onStatus: (status: ConnectionStatus, attempt: number) => void;
  readonly #baseBackoffMs: number;
  readonly #maxBackoffMs: number;

  /** Topic → handlers. The key set is also the resubscribe list after a drop. */
  readonly #handlers = new Map<Topic, Set<TopicHandler>>();
  /** Last delivered sequence per topic, retained across transport reconnects. */
  readonly #cursors = new Map<Topic, TopicCursor>();

  #socket: WebSocket | null = null;
  #status: ConnectionStatus = "idle";
  #attempt = 0;
  #notifiedAttempt = 0;
  #retryTimer: number | null = null;
  #disposed = false;

  constructor(options: WebSocketClientOptions) {
    this.#url = options.url;
    this.#onStatus = options.onStatus ?? (() => {});
    this.#baseBackoffMs = options.baseBackoffMs ?? DEFAULT_BASE_BACKOFF_MS;
    this.#maxBackoffMs = options.maxBackoffMs ?? DEFAULT_MAX_BACKOFF_MS;
  }

  get status(): ConnectionStatus {
    return this.#status;
  }

  connect(): void {
    if (this.#disposed || this.#socket) return;
    this.#clearRetryTimer();
    this.#setStatus(this.#attempt === 0 ? "connecting" : "reconnecting");

    const socket = new WebSocket(this.#url);
    this.#socket = socket;

    socket.onopen = () => {
      if (this.#socket !== socket) return;
      this.#attempt = 0;
      this.#setStatus("open");
      for (const topic of this.#handlers.keys()) {
        this.#sendSubscribe(topic);
      }
    };

    socket.onmessage = (event: MessageEvent<unknown>) => {
      if (this.#socket !== socket) return;
      const frame = decodeServerFrame(event.data);
      if (!frame) return;
      if (frame.type === "ready") {
        this.#cursors.set(frame.topic, {
          stream: frame.stream,
          seq: frame.cursor,
        });
        return;
      }
      if (frame.type !== "event" && frame.type !== "node_status") return;
      const cursor = this.#cursors.get(frame.topic);
      if (cursor?.stream === frame.stream && frame.seq <= cursor.seq) {
        return;
      }
      this.#cursors.set(frame.topic, { stream: frame.stream, seq: frame.seq });
      const handlers = this.#handlers.get(frame.topic);
      if (!handlers) return;
      for (const handler of handlers) handler(frame.payload, frame);
    };

    socket.onclose = () => {
      if (this.#socket !== socket) return;
      this.#socket = null;
      if (this.#disposed) {
        this.#setStatus("closed");
        return;
      }
      this.#scheduleReconnect();
    };

    // No `onerror` handler on purpose: an error is always followed by `close`,
    // and reconnecting from both would race two sockets.
  }

  /** Drop the connection and stop retrying. The instance is not reusable. */
  dispose(): void {
    this.#disposed = true;
    this.#clearRetryTimer();
    this.#handlers.clear();
    this.#cursors.clear();
    const socket = this.#socket;
    this.#socket = null;
    if (socket) {
      socket.onopen = null;
      socket.onmessage = null;
      socket.onclose = null;
      socket.onerror = null;
      socket.close();
    }
    this.#setStatus("closed");
  }

  /** Retry immediately instead of waiting out the current backoff. */
  reconnectNow(): void {
    if (this.#disposed || this.#socket) return;
    this.#attempt = 0;
    this.connect();
  }

  /**
   * Subscribe to a topic. Returns the unsubscribe function — call it from a
   * `useEffect` cleanup. The `unsubscribe` control frame is sent only when the
   * last handler for that topic goes away, so two components watching the same
   * run do not cancel each other.
   */
  subscribe(topic: Topic, handler: TopicHandler): () => void {
    let handlers = this.#handlers.get(topic);
    if (!handlers) {
      handlers = new Set();
      this.#handlers.set(topic, handlers);
      this.#sendSubscribe(topic);
    }
    handlers.add(handler);

    return () => {
      const current = this.#handlers.get(topic);
      if (!current) return;
      current.delete(handler);
      if (current.size > 0) return;
      this.#handlers.delete(topic);
      this.#cursors.delete(topic);
      this.#send({ type: "unsubscribe", topic });
    };
  }

  #send(frame: ClientFrame): void {
    // Nothing is queued while offline: on reopen every live topic is
    // resubscribed from `#handlers`, which is the authoritative set.
    if (this.#socket?.readyState !== WebSocket.OPEN) return;
    this.#socket.send(encodeClientFrame(frame));
  }

  #sendSubscribe(topic: Topic): void {
    const cursor = this.#cursors.get(topic);
    this.#send(
      cursor
        ? {
            type: "subscribe",
            topic,
            stream: cursor.stream,
            after: cursor.seq,
          }
        : { type: "subscribe", topic },
    );
  }

  #scheduleReconnect(): void {
    this.#attempt += 1;
    this.#setStatus("reconnecting");
    const ceiling = Math.min(
      this.#maxBackoffMs,
      this.#baseBackoffMs * 2 ** (this.#attempt - 1),
    );
    // Full jitter: a fixed ladder makes every reconnecting client hit the
    // backend in lockstep.
    const delay = Math.random() * ceiling;
    this.#retryTimer = window.setTimeout(() => {
      this.#retryTimer = null;
      this.connect();
    }, delay);
  }

  #clearRetryTimer(): void {
    if (this.#retryTimer === null) return;
    window.clearTimeout(this.#retryTimer);
    this.#retryTimer = null;
  }

  #setStatus(status: ConnectionStatus): void {
    if (this.#status === status && this.#notifiedAttempt === this.#attempt) {
      return;
    }
    this.#status = status;
    this.#notifiedAttempt = this.#attempt;
    this.#onStatus(status, this.#attempt);
  }
}

/** Same-origin `/ws`, so the Vite proxy and the packaged app agree. */
export function defaultWebSocketUrl(): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/ws`;
}
