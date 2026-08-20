// RTC log collection.
// RTC stats are sampled roughly every 2 seconds and are only good for trends;
// frozen frames and short response times need per-frame data from capture.ts.

export interface RtcSample {
  tsMonotonicMs: number;
  tsWallMs: number;
  resolution: { width: number; height: number } | null;
  bitrateBps: number | null;
  packetLossRatio: number | null;
  rttMs: number | null;
  bytesSent: number | null;
  bytesReceived: number | null;
}

export interface RtcStatsProvider {
  getStats(): Promise<RTCStatsReport | null>;
}

export class RtcLogCollector {
  private samples: RtcSample[] = [];
  private timer: ReturnType<typeof setInterval> | null = null;

  constructor(
    private readonly provider: RtcStatsProvider,
    private readonly intervalMs = 2000
  ) {}

  start(): void {
    if (this.timer) return;
    this.timer = setInterval(() => void this.sample(), this.intervalMs);
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  async sample(): Promise<RtcSample | null> {
    const report = await this.provider.getStats().catch(() => null);
    const sample = RtcLogCollector.parse(report, performance.now(), Date.now());
    if (sample) this.samples.push(sample);
    return sample;
  }

  static parse(
    report: RTCStatsReport | null,
    tsMonotonicMs: number,
    tsWallMs: number
  ): RtcSample | null {
    if (!report) return null;
    let bitrateBps: number | null = null;
    let packetLossRatio: number | null = null;
    let rttMs: number | null = null;
    let bytesSent: number | null = null;
    let bytesReceived: number | null = null;
    let resolution: { width: number; height: number } | null = null;
    report.forEach((stat) => {
      if (stat.type === "inbound-rtp" && stat.mediaType === "video") {
        if (typeof stat.bytesReceived === "number") bytesReceived = stat.bytesReceived;
        if (typeof stat.framesDecoded === "number" && typeof stat.framesPerSecond === "number") {
          resolution = {
            width: stat.frameWidth ?? 0,
            height: stat.frameHeight ?? 0,
          };
        }
        if (typeof stat.framesLost === "number" && typeof stat.framesReceived === "number") {
          const received = stat.framesReceived;
          if (received > 0) packetLossRatio = stat.framesLost / received;
        }
      }
      if (stat.type === "remote-inbound-rtp") {
        if (typeof stat.roundTripTime === "number") rttMs = stat.roundTripTime * 1000;
      }
      if (stat.type === "candidate-pair" && stat.nominated) {
        if (typeof stat.availableOutgoingBitrate === "number") bitrateBps = stat.availableOutgoingBitrate;
      }
      if (stat.type === "outbound-rtp" && stat.mediaType === "video") {
        if (typeof stat.bytesSent === "number") bytesSent = stat.bytesSent;
      }
    });
    return { tsMonotonicMs, tsWallMs, resolution, bitrateBps, packetLossRatio, rttMs, bytesSent, bytesReceived };
  }

  snapshot(): RtcSample[] {
    return this.samples.slice();
  }
}
