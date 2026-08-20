// Per-frame capture of input and remote output streams.
//
// Each frame records media time, arrival time, a cheap hash/similarity
// feature, the actual decoded width/height, and the recorded video. RTC logs
// are only ~2s trends; frozen/duplicate frames need this per-frame data.

export interface FrameRecord {
  stream: "input" | "output";
  mediaTimeMs: number;
  arrivalTimeMs: number;
  featureHash: string;
  width: number;
  height: number;
}

export class FrameCapture {
  private frames: FrameRecord[] = [];
  private videoTrack: MediaStreamTrack | null = null;
  private recorder: MediaRecorder | null = null;
  private chunks: Blob[] = [];

  attachVideo(track: MediaStreamTrack): void {
    this.videoTrack = track;
  }

  async startRecording(stream: MediaStream): Promise<void> {
    const candidates = stream.getVideoTracks();
    if (candidates.length === 0) return;
    this.attachVideo(candidates[0]);
    if (typeof MediaRecorder === "undefined") return;
    const recorder = new MediaRecorder(stream, { mimeType: "video/webm" });
    recorder.ondataavailable = (event: BlobEvent) => {
      if (event.data.size > 0) this.chunks.push(event.data);
    };
    recorder.start(250);
    this.recorder = recorder;
  }

  stopRecording(): Blob | null {
    if (this.recorder && this.recorder.state !== "inactive") {
      this.recorder.stop();
    }
    if (this.chunks.length === 0) return null;
    return new Blob(this.chunks, { type: "video/webm" });
  }

  /**
   * Sample one frame. The feature is a low-cost hash of a downscaled frame
   * so duplicate/frozen frames are detectable without heavy CV.
   */
  recordFrame(stream: "input" | "output", element: HTMLVideoElement): FrameRecord | null {
    const width = element.videoWidth || 0;
    const height = element.videoHeight || 0;
    const mediaTimeMs = element.currentTime * 1000;
    const record: FrameRecord = {
      stream,
      mediaTimeMs,
      arrivalTimeMs: performance.now(),
      featureHash: FrameCapture.frameHash(element),
      width,
      height,
    };
    this.frames.push(record);
    return record;
  }

  static frameHash(element: HTMLVideoElement): string {
    try {
      const canvas = document.createElement("canvas");
      canvas.width = 64;
      canvas.height = 64;
      const context = canvas.getContext("2d");
      if (!context) return "no-context";
      context.drawImage(element, 0, 0, 64, 64);
      const data = context.getImageData(0, 0, 64, 64).data;
      let hash = 0;
      for (let i = 0; i < data.length; i += 16) {
        hash = (hash * 31 + data[i]) >>> 0;
      }
      return hash.toString(16);
    } catch {
      return "undecodable";
    }
  }

  snapshot(): FrameRecord[] {
    return this.frames.slice();
  }
}
