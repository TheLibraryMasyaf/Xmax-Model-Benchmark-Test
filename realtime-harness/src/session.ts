// Session manager for the XMAX realtime SDK.
//
// Callback registration is mandatory: onRemoteStream, onStateChange, onError,
// onDisconnect and onRoomEvent. Every callback records both a monotonic clock
// timestamp and a wall-clock timestamp. Audio is explicitly published and
// subscribed. Audio preservation is only assessable when the returned remote
// MediaStream actually contains an audio track.
//
// A fixed Feed used with auto-looping input must be limited to a single round
// window; call stopGeneration() at the end of each round.

import { EventScriptRunner, StreamSettings, TracksFrame } from "./events.js";

export type SessionState = "idle" | "running" | "disconnected";

export interface TimestampedCallback {
  tsMonotonicMs: number;
  tsWallMs: number;
  [key: string]: unknown;
}

export interface SdkCallbacks {
  onRemoteStream?: (stream: MediaStream) => void;
  onStateChange?: (state: SessionState) => void;
  onError?: (error: Error) => void;
  onDisconnect?: (reason: string) => void;
  onRoomEvent?: (event: string, payload: Record<string, unknown>) => void;
}

export interface SdkClient {
  connectMedia(media: MediaStream, options: unknown): Promise<{ sessionUid: string; media: { streamSetting: StreamSettings } }>;
  connectCamera(options: unknown): Promise<{ sessionUid: string; media: { streamSetting: StreamSettings } }>;
  connect(stream: MediaStream, options: unknown): Promise<{ sessionUid: string; media: { streamSetting: StreamSettings } }>;
  set(context: Record<string, unknown>): Promise<void>;
  start(context?: Record<string, unknown>): Promise<void>;
  stopGeneration(): Promise<void>;
  disconnect(): Promise<void>;
}

export class XmaxSessionManager {
  private callbacks: SdkCallbacks = {};
  private sessionUid: string | null = null;
  private callLog: TimestampedCallback[] = [];

  constructor(private readonly client: SdkClient) {}

  on(callbacks: SdkCallbacks): void {
    this.callbacks = callbacks;
  }

  private emit(name: string, payload: Record<string, unknown> = {}): void {
    const entry: TimestampedCallback = {
      tsMonotonicMs: performance.now(),
      tsWallMs: Date.now(),
      ...payload,
    };
    this.callLog.push({ tsMonotonicMs: entry.tsMonotonicMs, tsWallMs: entry.tsWallMs, callback: name });
    switch (name) {
      case "onRemoteStream":
        this.callbacks.onRemoteStream?.(payload["stream"] as MediaStream);
        break;
      case "onStateChange":
        this.callbacks.onStateChange?.(payload["state"] as SessionState);
        break;
      case "onError":
        this.callbacks.onError?.(payload["error"] as Error);
        break;
      case "onDisconnect":
        this.callbacks.onDisconnect?.((payload["reason"] as string) ?? "unknown");
        break;
      case "onRoomEvent":
        this.callbacks.onRoomEvent?.((payload["event"] as string) ?? "", payload["payload"] as Record<string, unknown>);
        break;
    }
  }

  async connect(kind: "connectMedia" | "connectCamera" | "connect", input: MediaStream | null, options: unknown = {}): Promise<{ sessionUid: string; media: { streamSetting: StreamSettings } }> {
    this.emit("connect_call", { kind });
    const opts = (options ?? {}) as Record<string, unknown>;
    const audioOptions = { audio: { publish: true, subscribe: true } };
    let session: { sessionUid: string; media: { streamSetting: StreamSettings } };
    if (kind === "connectCamera") {
      session = await this.client.connectCamera({ ...opts, ...audioOptions });
    } else if (kind === "connectMedia" && input) {
      session = await this.client.connectMedia(input, { ...opts, ...audioOptions });
    } else if (input) {
      session = await this.client.connect(input, { ...opts, ...audioOptions });
    } else {
      throw new Error(`connect kind ${kind} requires an input stream`);
    }
    this.sessionUid = session.sessionUid;
    this.emit("connect_completed", { sessionUid: session.sessionUid });
    return session;
  }

  async set(context: Record<string, unknown>): Promise<void> {
    this.emit("set_called", {});
    await this.client.set(context);
  }

  async start(context?: Record<string, unknown>): Promise<void> {
    this.emit("start_called", {});
    await this.client.start(context);
  }

  async stopGeneration(): Promise<void> {
    this.emit("stop_called", {});
    await this.client.stopGeneration();
  }

  async disconnect(): Promise<void> {
    if (!this.sessionUid) return;
    this.emit("disconnect_called", { sessionUid: this.sessionUid });
    await this.client.disconnect();
    this.sessionUid = null;
  }

  getSessionUid(): string | null {
    return this.sessionUid;
  }

  callLogSnapshot(): TimestampedCallback[] {
    return this.callLog.slice();
  }

  static withCallbacks(
    client: SdkClient,
    callbacks: SdkCallbacks,
    runner: EventScriptRunner
  ): XmaxSessionManager {
    const manager = new XmaxSessionManager(client);
    manager.on(callbacks);
    return manager;
  }
}

export type { EventScriptRunner, TracksFrame };
