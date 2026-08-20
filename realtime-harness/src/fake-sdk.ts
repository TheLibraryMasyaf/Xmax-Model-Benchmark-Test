// Fake XMAX SDK for offline harness tests.
//
// Implements the same SdkClient surface as the real SDK: connectMedia /
// connectCamera / connect, set/start/stopGeneration/disconnect, audio
// publish/subscribe options, callbacks with timestamps, and scriptable
// disconnect/reconnect. No real key or WebRTC peer is required.

import type { SdkClient, SessionState } from "./session.js";
import { EventScriptRunner } from "./events.js";

export interface FakeSdkOptions {
  sessionUid?: string;
  contentWidth?: number;
  contentHeight?: number;
  disconnectOnConnect?: boolean;
  reconnectAfterMs?: number;
  rounds?: number;
}

interface FakeSession {
  sessionUid: string;
  media: { streamSetting: { width: number; height: number } };
}

export class FakeXmaxSdk implements SdkClient {
  readonly options: Required<FakeSdkOptions>;
  private state: SessionState = "idle";
  private callbacks: {
    onRemoteStream?: (stream: MediaStream) => void;
    onStateChange?: (state: SessionState) => void;
    onError?: (error: Error) => void;
    onDisconnect?: (reason: string) => void;
    onRoomEvent?: (event: string, payload: Record<string, unknown>) => void;
  } = {};
  startCount = 0;
  stopCount = 0;
  disconnectCount = 0;
  mediaStreams: MediaStream[] = [];
  callLog: string[] = [];

  constructor(options: FakeSdkOptions = {}) {
    this.options = {
      sessionUid: options.sessionUid ?? "fake-session-1",
      contentWidth: options.contentWidth ?? 1280,
      contentHeight: options.contentHeight ?? 720,
      disconnectOnConnect: options.disconnectOnConnect ?? false,
      reconnectAfterMs: options.reconnectAfterMs ?? 0,
      rounds: options.rounds ?? 1,
    };
  }

  on(callbacks: typeof this.callbacks): void {
    this.callbacks = callbacks;
  }

  async connectMedia(media: MediaStream, options: unknown): Promise<FakeSession> {
    return this.connect(media, options);
  }

  async connectCamera(options: unknown): Promise<FakeSession> {
    return this.connect(null, options);
  }

  async connect(stream: MediaStream | null, options: unknown): Promise<FakeSession> {
    this.callLog.push("connect");
    const audio = (options as { audio?: { publish?: boolean; subscribe?: boolean } })?.audio;
    this.lastAudioOptions = audio ?? null;
    if (stream) this.mediaStreams.push(stream);
    if (this.options.disconnectOnConnect) {
      setTimeout(() => {
        this.state = "disconnected";
        this.callbacks.onDisconnect?.("simulated-network-loss");
        this.callbacks.onStateChange?.("disconnected");
        if (this.options.reconnectAfterMs > 0) {
          setTimeout(() => {
            this.state = "running";
            this.callbacks.onStateChange?.("running");
            this.callbacks.onRoomEvent?.("video_started", {});
          }, this.options.reconnectAfterMs);
        }
      }, 0);
    }
    return {
      sessionUid: this.options.sessionUid,
      media: {
        streamSetting: { width: this.options.contentWidth, height: this.options.contentHeight },
      },
    };
  }

  lastAudioOptions: { publish?: boolean; subscribe?: boolean } | null = null;

  async set(_context: Record<string, unknown>): Promise<void> {
    this.callLog.push("set");
  }

  async start(_context?: Record<string, unknown>): Promise<void> {
    this.startCount += 1;
    this.callLog.push("start");
    this.state = "running";
    this.callbacks.onStateChange?.("running");
    this.callbacks.onRoomEvent?.("video_started", {});
  }

  async stopGeneration(): Promise<void> {
    this.stopCount += 1;
    this.callLog.push("stopGeneration");
  }

  async disconnect(): Promise<void> {
    this.disconnectCount += 1;
    this.callLog.push("disconnect");
    this.state = "disconnected";
    this.callbacks.onDisconnect?.("client-disconnect");
  }

  getState(): SessionState {
    return this.state;
  }
}

export function createFakeRunner(options?: FakeSdkOptions): {
  sdk: FakeXmaxSdk;
  runner: EventScriptRunner;
} {
  const sdk = new FakeXmaxSdk(options);
  const runner = new EventScriptRunner(
    {
      async sendTracks(points) {
        return { delivered: points.length > 0 };
      },
    },
    { width: options?.contentWidth ?? 1280, height: options?.contentHeight ?? 720 },
    { width: 640, height: 360 }
  );
  return { sdk, runner };
}
