import { afterEach, describe, expect, it, vi } from "vitest";
import { WebSocketClient } from "@/ws/client";
import { METRICS_TOPIC } from "@/ws/protocol";

class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  readonly sent: string[] = [];
  readyState = FakeWebSocket.CONNECTING;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent<unknown>) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readonly url: string;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.(new Event("open"));
  }

  receive(data: string): void {
    this.onmessage?.(new MessageEvent("message", { data }));
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.readyState = FakeWebSocket.CLOSED;
  }
}

afterEach(() => {
  FakeWebSocket.instances = [];
  vi.unstubAllGlobals();
});

describe("WebSocket metrics multiplexing", () => {
  it("delivers metrics through the shared connection and records its cursor", () => {
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const client = new WebSocketClient({ url: "ws://agenthub.test/ws" });
    const received: number[] = [];

    client.connect();
    const socket = FakeWebSocket.instances[0];
    expect(socket).toBeDefined();
    socket?.open();
    const unsubscribe = client.subscribe(METRICS_TOPIC, (_payload, frame) => {
      if (frame.type === "metrics") received.push(frame.payload.cpu_percent);
    });

    expect(socket?.sent).toEqual([
      JSON.stringify({ type: "subscribe", topic: "metrics" }),
    ]);
    socket?.receive(
      JSON.stringify({
        type: "metrics",
        stream: "stream_one",
        topic: "metrics",
        seq: 4,
        payload: {
          ts: 20,
          cpu_percent: 34.5,
          cpu_per_core: [20, 49],
          memory_total_bytes: 1_000,
          memory_used_bytes: 600,
          memory_available_bytes: 400,
          memory_percent: 60,
          swap_total_bytes: 0,
          swap_used_bytes: 0,
          swap_free_bytes: 0,
          swap_percent: 0,
          disk_total_bytes: 2_000,
          disk_used_bytes: 500,
          disk_free_bytes: 1_500,
          disk_percent: 25,
          processes: [],
        },
      }),
    );
    // A duplicate sequence from the same stream must not update live state.
    socket?.receive(
      JSON.stringify({
        type: "metrics",
        stream: "stream_one",
        topic: "metrics",
        seq: 4,
        payload: {
          ts: 21,
          cpu_percent: 99,
          cpu_per_core: [],
          memory_total_bytes: 0,
          memory_used_bytes: 0,
          memory_available_bytes: 0,
          memory_percent: 0,
          swap_total_bytes: 0,
          swap_used_bytes: 0,
          swap_free_bytes: 0,
          swap_percent: 0,
          disk_total_bytes: 0,
          disk_used_bytes: 0,
          disk_free_bytes: 0,
          disk_percent: 0,
          processes: [],
        },
      }),
    );

    expect(received).toEqual([34.5]);
    unsubscribe();
    client.dispose();
  });
});
