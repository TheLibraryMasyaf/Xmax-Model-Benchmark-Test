#!/usr/bin/env node
// Browser-based XMAX realtime harness. Reads a JSON config and writes a JSON result.

import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";
import { chromium } from "playwright";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const args = Object.fromEntries(process.argv.slice(2).map((value, index, all) => {
  if (!value.startsWith("--")) return ["", ""];
  const [key, inline] = value.slice(2).split("=", 2);
  return [key, inline ?? all[index + 1]];
}).filter(([key]) => key));
if (!args.config) throw new Error("usage: node src/harness.js --config <config.json>");
const config = JSON.parse(fs.readFileSync(args.config, "utf8"));
const apiKey = process.env.XMAX_API_KEY;
if (!apiKey) throw new Error("XMAX_API_KEY is required");
const sdkPackage = JSON.parse(fs.readFileSync(path.join(root, "node_modules", "@xmaxai", "sdk", "package.json"), "utf8"));
const sdkBundleDirectory = path.join(root, "var", "sdk");
const sdkBundle = path.join(sdkBundleDirectory, `xmax-sdk-${sdkPackage.version}.js`);
if (!fs.existsSync(sdkBundle)) {
  fs.mkdirSync(sdkBundleDirectory, { recursive: true });
  await build({
    entryPoints: [path.join(root, "node_modules", "@xmaxai", "sdk", "dist", "index.js")],
    outfile: sdkBundle,
    bundle: true,
    platform: "browser",
    format: "esm",
    target: ["chrome120"],
    sourcemap: false,
    legalComments: "none",
  });
}

function mime(file) {
  if (file.endsWith(".js")) return "text/javascript";
  if (file.endsWith(".mp4")) return "video/mp4";
  if (file.endsWith(".webm")) return "video/webm";
  if (/\.jpe?g$/i.test(file)) return "image/jpeg";
  if (file.endsWith(".png")) return "image/png";
  // Assets in the test database are stored as extension-less ``source.bin``
  // blobs.  Fall back to content sniffing so the SDK receives the correct
  // media type instead of application/octet-stream (which it rejects).
  try {
    const fd = fs.openSync(file, "r");
    const head = Buffer.alloc(16);
    fs.readSync(fd, head, 0, head.length, 0);
    fs.closeSync(fd);
    if (head.subarray(4, 8).toString("ascii") === "ftyp") return "video/mp4";
    if (head.subarray(0, 4).equals(Buffer.from([0x1a, 0x45, 0xdf, 0xa3]))) return "video/webm";
    if (head.subarray(0, 2).equals(Buffer.from([0xff, 0xd8]))) return "image/jpeg";
    if (head.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) return "image/png";
  } catch (error) {
    // ignore; fall through to octet-stream
  }
  return "application/octet-stream";
}

const server = http.createServer((request, response) => {
  let file;
  // /input.mp4 serves the same media as /input but with a .mp4 suffix so the
  // SDK's isVideoMediaFile() URL check passes, letting createVideoFileStream
  // load it over HTTP (blob URLs for large MP4s fail to decode in headless
  // Chrome with MEDIA_ELEMENT_ERROR: Format error).
  if (request.url === "/input" || request.url === "/input.mp4") file = config.input_path;
  else if (request.url === "/reference") file = config.reference_path;
  else if (request.url === "/sdk/index.js") file = sdkBundle;
  else {
    response.writeHead(200, { "Content-Type": "text/html" });
    response.end('<video id="input" muted playsinline></video><div id="local-host"></div><div id="remote-host"></div><video id="remote" playsinline></video>');
    return;
  }
  if (!file || !fs.existsSync(file)) {
    response.writeHead(404); response.end(); return;
  }
  response.writeHead(200, { "Content-Type": mime(file), "Access-Control-Allow-Origin": "*" });
  fs.createReadStream(file).pipe(response);
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const port = server.address().port;
const browser = await chromium.launch({ headless: config.headed !== true, channel: "chrome" });
try {
  const page = await browser.newPage({ permissions: ["camera", "microphone"] });
  const browserLogs = [];
  page.on("console", (message) => browserLogs.push({
    level: message.type(), message: message.text(), tsWallMs: Date.now(),
  }));
  await page.goto(`http://127.0.0.1:${port}/`);
  const result = await page.evaluate(async ({ apiKey, config, sdkVersion }) => {
    const callbacks = [];
    const events = [];
    const frames = [];
    const rtcLog = [];
    const stateChanges = [];
    const stamp = (callback, payload = {}) => callbacks.push({ callback, tsMonotonicMs: performance.now(), tsWallMs: Date.now(), ...payload });
    const frameFeatureHash = (video) => {
      try {
        const canvas = document.createElement("canvas");
        canvas.width = 8; canvas.height = 8;
        const context = canvas.getContext("2d", { willReadFrequently: true });
        if (!context || !video.videoWidth || !video.videoHeight) return null;
        context.drawImage(video, 0, 0, 8, 8);
        const pixels = context.getImageData(0, 0, 8, 8).data;
        const luma = [];
        for (let index = 0; index < pixels.length; index += 4) {
          luma.push(Math.round(0.299 * pixels[index] + 0.587 * pixels[index + 1] + 0.114 * pixels[index + 2]));
        }
        const mean = luma.reduce((sum, value) => sum + value, 0) / luma.length;
        let bits = "";
        for (const value of luma) bits += value >= mean ? "1" : "0";
        return BigInt(`0b${bits}`).toString(16).padStart(16, "0");
      } catch {
        return null;
      }
    };
    const hashDistance = (left, right) => {
      if (!left || !right || left.length !== right.length) return null;
      let distance = 0;
      for (let index = 0; index < left.length; index += 1) {
        let value = parseInt(left[index], 16) ^ parseInt(right[index], 16);
        while (value) { distance += value & 1; value >>>= 1; }
      }
      return distance;
    };

    // Keep references to browser peer connections so short RTC changes are not
    // reduced to the SDK's coarse console diagnostics.
    const peerConnections = [];
    const NativePeerConnection = window.RTCPeerConnection;
    if (NativePeerConnection) {
      const TrackingPeerConnection = function (...args) {
        const peer = new NativePeerConnection(...args);
        peerConnections.push(peer);
        return peer;
      };
      TrackingPeerConnection.prototype = NativePeerConnection.prototype;
      Object.setPrototypeOf(TrackingPeerConnection, NativePeerConnection);
      window.RTCPeerConnection = TrackingPeerConnection;
    }
    const snapshotRtc = async () => {
      for (const [peerIndex, peer] of peerConnections.entries()) {
        try {
          const report = await peer.getStats();
          const entries = [];
          report.forEach((entry) => {
            if (["inbound-rtp", "outbound-rtp", "remote-inbound-rtp", "candidate-pair"].includes(entry.type)) {
              entries.push(Object.fromEntries(Object.entries(entry)));
            }
          });
          rtcLog.push({ tsMonotonicMs: performance.now(), tsWallMs: Date.now(), peerIndex, entries });
        } catch (error) {
          rtcLog.push({ tsMonotonicMs: performance.now(), tsWallMs: Date.now(), peerIndex, error: String(error) });
        }
      }
    };

    const sdk = await import("/sdk/index.js");
    const input = document.querySelector("#input");
    const remoteVideo = document.querySelector("#remote");
    remoteVideo.autoplay = true;
    const client = sdk.createXmaxClient({
      apiKey,
      baseUrl: config.base_url,
      onError: (message, error) => stamp("onError", { message, error: String(error ?? "") }),
      onLog: (entry) => callbacks.push({ callback: "sdk_log", tsMonotonicMs: performance.now(), tsWallMs: Date.now(), entry }),
    });
    let referenceUrl = config.reference_url;
    if (config.reference_path) {
      const referenceResponse = await fetch("/reference");
      if (!referenceResponse.ok) throw new Error(`reference fetch failed: ${referenceResponse.status}`);
      const referenceBlob = await referenceResponse.blob();
      const referenceFile = new File(
        [referenceBlob],
        config.reference_filename ?? "reference.png",
        { type: referenceBlob.type || "image/png" },
      );
      const uploadedReference = await client.files.uploadImage(referenceFile);
      referenceUrl = uploadedReference.url;
      stamp("reference_uploaded", { bytes: referenceBlob.size });
    }
    let receivedRemoteStream = null;
    const connectionCallbacks = {
      onRemoteStream: (stream) => {
        receivedRemoteStream = stream;
        remoteVideo.srcObject = stream;
        void remoteVideo.play();
        stamp("onRemoteStream");
      },
      onStateChange: (state) => {
        const entry = { state, tsMonotonicMs: performance.now(), tsWallMs: Date.now() };
        stateChanges.push(entry);
        stamp("onStateChange", { state });
      },
      onError: (message, error) => stamp("onError", { message, error: String(error ?? "") }),
      onDisconnect: (reason) => stamp("onDisconnect", { reason }),
      onRoomEvent: (event) => stamp("onRoomEvent", { event }),
    };
    const model = sdk.models.realtime(config.model ?? "x2.0");
    const context = { prompt: config.prompt ?? "", ...(referenceUrl ? { refImageUrl: referenceUrl } : {}) };
    let source = null;
    let session;
    const inputMethod = config.input_method ?? "connectMedia";
    stamp("connect_call", { inputMethod });
    if (inputMethod === "connectCamera" && typeof client.realtime.connectCamera === "function") {
      session = await client.realtime.connectCamera({
        model,
        facingMode: config.facing_mode ?? "user",
        context,
        render: {
          localContainer: document.querySelector("#local-host"),
          remoteContainer: document.querySelector("#remote-host"),
          fit: "contain",
          drag: { enabled: false },
        },
        audio: { publish: true, subscribe: true },
        log: { rtc: true },
        autoStart: true,
        ...connectionCallbacks,
      });
    } else if (inputMethod === "connect" && typeof client.realtime.connect === "function" && typeof client.realtime.connectMedia === "function") {
      input.src = "/input";
      input.loop = false;
      await input.play();
      const ownedStream = input.captureStream();
      session = await client.realtime.connect(ownedStream, {
        model,
        context,
        render: { remoteContainer: document.querySelector("#remote-host"), fit: "contain", drag: { enabled: false } },
        audio: { publish: true, subscribe: true },
        log: { rtc: true },
        autoStart: true,
        ...connectionCallbacks,
      });
      source = { previewStream: ownedStream, destroy: () => ownedStream.getTracks().forEach((track) => track.stop()) };
    } else if (typeof client.realtime.connectMedia === "function") {
      // Pass the media URL directly instead of fetching it into a Blob: the
      // SDK loads URLs over HTTP (crossOrigin=anonymous) which decodes
      // reliably, whereas Blob-URL playback of MP4 fails in headless Chrome.
      session = await client.realtime.connectMedia("/input.mp4", {
        model,
        playback: { playbackRate: 1 },
        context,
        render: {
          localContainer: document.querySelector("#local-host"),
          remoteContainer: document.querySelector("#remote-host"),
          fit: "contain",
          drag: { enabled: false },
        },
        audio: { publish: true, subscribe: true },
        log: { rtc: true },
        autoStart: true,
        ...connectionCallbacks,
      });
    } else {
      // Compatibility with @xmaxai/sdk 0.1.x, whose public surface only
      // exposed connect(MediaStream, initialState).
      source = await mediaSdk.createVideoFileStream("/input", { loop: false });
      session = await client.realtime.connect(source.previewStream, {
        model,
        localElement: document.querySelector("#local-host"),
        remoteElement: document.querySelector("#remote-host"),
        inputKind: "file",
        initialState: {
          prompt: { text: context.prompt },
          image: referenceUrl,
          autoStart: true,
        },
        drag: false,
        ...connectionCallbacks,
      });
    }
    const sessionUid = session.getSessionUid();
    stamp("connect_completed", { sessionUid });
    const inputStream = session.media?.stream ?? session.getInputPreviewStream?.() ?? source?.previewStream;
    if (inputStream) {
      input.srcObject = inputStream;
      await input.play();
    }
    const deadline = performance.now() + 30000;
    while (performance.now() < deadline) {
      if (remoteVideo?.srcObject && remoteVideo.readyState >= 2) break;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    const remoteStream = receivedRemoteStream ?? remoteVideo?.srcObject;
    if (!remoteStream) throw new Error("remote output stream did not become available");
    stamp("first_decoded_frame", { mediaTimeMs: remoteVideo.currentTime * 1000 });
    const recorder = new MediaRecorder(remoteStream, { mimeType: "video/webm" });
    const chunks = [];
    recorder.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data); };
    recorder.start(250);
    const started = performance.now();
    events.push({ event: "task_start", plannedMs: 0, executedMs: 0, payload: {} });
    const scripted = config.tracks ?? [];
    let trackIndex = 0;
    let lastOutputFeatureHash = null;
    const pendingResponseProbes = [];
    while ((performance.now() - started) < Number(config.duration_s ?? 3) * 1000) {
      const now = performance.now() - started;
      const outputArrival = performance.now();
      const outputFeatureHash = frameFeatureHash(remoteVideo);
      frames.push({ stream: "output", mediaTimeMs: remoteVideo.currentTime * 1000, arrivalTimeMs: outputArrival, featureHash: outputFeatureHash, width: remoteVideo.videoWidth, height: remoteVideo.videoHeight });
      for (const probe of pendingResponseProbes) {
        if (probe.firstOutputChangeMs != null) continue;
        const distance = hashDistance(probe.baselineFeatureHash, outputFeatureHash);
        if (distance != null && distance >= 8) {
          probe.firstOutputChangeMs = outputArrival - probe.inputMonotonicMs;
        }
      }
      lastOutputFeatureHash = outputFeatureHash ?? lastOutputFeatureHash;
      if (input.srcObject) {
        frames.push({ stream: "input", mediaTimeMs: input.currentTime * 1000, arrivalTimeMs: performance.now(), featureHash: frameFeatureHash(input), width: input.videoWidth, height: input.videoHeight });
      }
      while (
        trackIndex < scripted.length
        && (performance.now() - started) >= Number(scripted[trackIndex].at_ms ?? 0)
      ) {
        const swipeId = scripted[trackIndex].swipe_id ?? null;
        const phase = scripted[trackIndex].phase ?? "move";
        if (phase === "start") {
          events.push({ event: "drag_start", plannedMs: scripted[trackIndex].at_ms ?? now, executedMs: performance.now() - started, payload: { swipeId } });
        }
        const points = scripted[trackIndex].points ?? [];
        let sendResult = "sent";
        const inputMonotonicMs = performance.now();
        try { await session.sendTracks(points); } catch { sendResult = "ignored"; }
        const responseProbe = phase === "start" || swipeId == null;
        const trackEvent = { event: "tracks_frame", plannedMs: scripted[trackIndex].at_ms ?? now, executedMs: inputMonotonicMs - started, inputMonotonicMs, contentCoords: points, swipeId, phase, sendResult, responseProbe, baselineFeatureHash: lastOutputFeatureHash, firstOutputChangeMs: null };
        events.push(trackEvent);
        if (responseProbe && sendResult === "sent" && lastOutputFeatureHash) pendingResponseProbes.push(trackEvent);
        if (phase === "end") {
          events.push({ event: "drag_end", plannedMs: scripted[trackIndex].at_ms ?? now, executedMs: performance.now() - started, payload: { swipeId } });
        }
        trackIndex += 1;
      }
      if (Math.floor(now / 1000) !== Math.floor((now - 1000 / 30) / 1000)) await snapshotRtc();
      await new Promise((resolve) => setTimeout(resolve, 1000 / 30));
    }
    events.push({ event: "task_stop", plannedMs: performance.now() - started, executedMs: performance.now() - started, payload: {} });
    const recorderStopped = new Promise((resolve) => { recorder.onstop = resolve; });
    recorder.stop();
    await recorderStopped;
    if (typeof session.stopGeneration === "function") await session.stopGeneration();
    else await session.stop();
    const blob = new Blob(chunks, { type: "video/webm" });
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
    }
    const recordingBase64 = btoa(binary);
    await session.disconnect();
    source?.destroy?.();
    const connectCall = callbacks.find((item) => item.callback === "connect_call")?.tsMonotonicMs;
    const firstDecoded = callbacks.find((item) => item.callback === "first_decoded_frame")?.tsMonotonicMs;
    const outputFrames = frames.filter((item) => item.stream === "output");
    const frameIntervals = outputFrames.slice(1).map(
      (item, index) => item.arrivalTimeMs - outputFrames[index].arrivalTimeMs,
    );
    const expectedIntervalMs = 1000 / 30;
    const droppedFrames = frameIntervals.reduce(
      (sum, interval) => sum + Math.max(0, Math.round(interval / expectedIntervalMs) - 1),
      0,
    );
    const windowCounts = new Map();
    for (const frame of outputFrames) {
      const window = Math.floor((frame.arrivalTimeMs - started) / 1000);
      windowCounts.set(window, (windowCounts.get(window) ?? 0) + 1);
    }
    const counts = [...windowCounts.values()];
    const meanWindowFps = counts.length ? counts.reduce((sum, value) => sum + value, 0) / counts.length : 0;
    const fpsWindowCv = meanWindowFps && counts.length > 1
      ? Math.sqrt(counts.reduce((sum, value) => sum + (value - meanWindowFps) ** 2, 0) / counts.length) / meanWindowFps
      : 0;
    const trackEvents = events.filter((item) => item.event === "tracks_frame");
    const sentTracks = trackEvents.filter((item) => item.sendResult === "sent").length;
    return {
      sdk_version: sdkVersion,
      session_uid: sessionUid,
      callbacks, events, frames, rtc_log: rtcLog, state_changes: stateChanges,
      audio: {
        publish_requested: true,
        subscribe_requested: true,
        publish: Boolean(inputStream?.getAudioTracks().length),
        subscribe: remoteStream.getAudioTracks().length > 0,
        input_track_count: inputStream?.getAudioTracks().length ?? 0,
        remote_track_count: remoteStream.getAudioTracks().length,
      },
      single_round: true,
      stream_setting: session.media?.streamSetting ?? { width: remoteVideo.videoWidth, height: remoteVideo.videoHeight },
      recording_base64: recordingBase64,
      metrics: {
        frames_captured: outputFrames.length,
        fps: outputFrames.length / Number(config.duration_s ?? 3),
        dropped_frames: droppedFrames,
        fps_window_cv: fpsWindowCv,
        session_duration_s: Number(config.duration_s ?? 3),
        track_send_success_ratio: trackEvents.length ? sentTracks / trackEvents.length : null,
        first_frame_ms: connectCall != null && firstDecoded != null ? firstDecoded - connectCall : null,
      },
    };
  }, { apiKey, config, sdkVersion: sdkPackage.version });
  result.browser_log = browserLogs;
  if (config.output_video_path && result.recording_base64) {
    fs.writeFileSync(config.output_video_path, Buffer.from(result.recording_base64, "base64"));
    result.recording_path = config.output_video_path;
    delete result.recording_base64;
  }
  fs.writeFileSync(config.output_json_path, JSON.stringify(result, null, 2));
} finally {
  await browser.close();
  server.close();
}
