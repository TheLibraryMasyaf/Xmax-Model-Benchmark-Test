// Controlled event scripts and track-sending.
//
// Custom tracks are sent at roughly 30 FPS. Single-finger is [[x,y]],
// multi-finger is [[x1,y1],[x2,y2],...]. Coordinates are based on the
// streamSetting *content resolution*, not the DOM container, and range from
// [0,0] to [width-1,height-1].

export type TrackPoint = [number, number];

export interface TracksFrame {
  event: "tracks_frame";
  plannedMs: number;
  executedMs: number;
  screenCoords: TrackPoint[];
  contentCoords: TrackPoint[];
  sendResult: "sent" | "dropped" | "ignored";
  responseProbe: boolean;
  inputMonotonicMs: number;
  baselineFeatureHash: string | null;
  firstOutputChangeMs: number | null;
}

export interface HarnessEvent {
  event:
    | "context_set"
    | "task_start"
    | "drag_start"
    | "tracks_frame"
    | "drag_end"
    | "task_stop";
  plannedMs: number;
  executedMs: number;
  payload: Record<string, unknown>;
}

export interface StreamSettings {
  width: number;
  height: number;
}

export function mapToContent(
  point: TrackPoint,
  content: StreamSettings,
  dom: StreamSettings
): TrackPoint {
  const x = Math.round(
    Math.min(Math.max((point[0] / Math.max(1, dom.width)) * content.width, 0), content.width - 1)
  );
  const y = Math.round(
    Math.min(Math.max((point[1] / Math.max(1, dom.height)) * content.height, 0), content.height - 1)
  );
  return [x, y];
}

export interface TrackSender {
  sendTracks(points: TrackPoint[]): Promise<{ delivered: boolean }>;
}

export class EventScriptRunner {
  private events: HarnessEvent[] = [];
  private startWallMs = 0;

  constructor(
    private readonly sender: TrackSender,
    private readonly content: StreamSettings,
    private readonly dom: StreamSettings
  ) {}

  startClock(): void {
    this.startWallMs = performance.now();
  }

  nowMs(): number {
    return performance.now() - this.startWallMs;
  }

  contextSet(payload: Record<string, unknown>): HarnessEvent {
    return this.record("context_set", payload);
  }

  taskStart(payload: Record<string, unknown>): HarnessEvent {
    return this.record("task_start", payload);
  }

  dragStart(payload: Record<string, unknown>): HarnessEvent {
    return this.record("drag_start", payload);
  }

  dragEnd(payload: Record<string, unknown>): HarnessEvent {
    return this.record("drag_end", payload);
  }

  taskStop(payload: Record<string, unknown>): HarnessEvent {
    return this.record("task_stop", payload);
  }

  private record(
    event: HarnessEvent["event"],
    payload: Record<string, unknown>
  ): HarnessEvent {
    const item: HarnessEvent = {
      event,
      plannedMs: this.nowMs(),
      executedMs: this.nowMs(),
      payload,
    };
    this.events.push(item);
    return item;
  }

  /**
   * Replay a scripted list of frame lists at ~30 FPS, mapping screen
   * coordinates to content coordinates and recording the send result.
   * Returns the recorded tracks_frames for verification.
   */
  async playTracks(frames: TrackPoint[][], fps = 30): Promise<TracksFrame[]> {
    const intervalMs = 1000 / fps;
    const out: TracksFrame[] = [];
    for (let i = 0; i < frames.length; i++) {
      const plannedMs = i * intervalMs;
      const screenCoords = frames[i];
      const contentCoords = screenCoords.map((p) =>
        mapToContent(p, this.content, this.dom)
      );
      const executedMs = this.nowMs();
      let sendResult: TracksFrame["sendResult"] = "sent";
      let delivered = false;
      try {
        const response = await this.sender.sendTracks(contentCoords);
        delivered = response.delivered;
        sendResult = delivered ? "sent" : "dropped";
      } catch {
        sendResult = "ignored";
      }
      const frame: TracksFrame = {
        event: "tracks_frame",
        plannedMs,
        executedMs,
        screenCoords,
        contentCoords,
        sendResult,
        responseProbe: i === 0,
        inputMonotonicMs: performance.now(),
        baselineFeatureHash: null,
        firstOutputChangeMs: null,
      };
      this.events.push(frame as unknown as HarnessEvent);
      out.push(frame);
      await this.delayUntil(plannedMs);
    }
    return out;
  }

  private delayUntil(plannedMs: number): Promise<void> {
    const wait = plannedMs - this.nowMs();
    if (wait <= 0) return Promise.resolve();
    return new Promise((resolve) => setTimeout(resolve, wait));
  }

  snapshot(): HarnessEvent[] {
    return this.events.slice();
  }
}
