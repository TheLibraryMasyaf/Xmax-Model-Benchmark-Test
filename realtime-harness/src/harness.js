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
  return "application/octet-stream";
}

const server = http.createServer((request, response) => {
  let file;
  if (request.url === "/input") file = config.input_path;
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
const browser = await chromium.launch({ headless: config.headed !== true });
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
      const response = await fetch("/input");
      const inputBlob = await response.blob();
      session = await client.realtime.connectMedia(inputBlob, {
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
    while ((performance.now() - started) < Number(config.duration_s ?? 3) * 1000) {
      const now = performance.now() - started;
      if (trackIndex < scripted.length && now >= Number(scripted[trackIndex].at_ms ?? 0)) {
        const points = scripted[trackIndex].points ?? [];
        let sendResult = "sent";
        try { await session.sendTracks(points); } catch { sendResult = "ignored"; }
        events.push({ event: "tracks_frame", plannedMs: scripted[trackIndex].at_ms ?? now, executedMs: now, contentCoords: points, sendResult });
        trackIndex += 1;
      }
      frames.push({ stream: "output", mediaTimeMs: remoteVideo.currentTime * 1000, arrivalTimeMs: performance.now(), width: remoteVideo.videoWidth, height: remoteVideo.videoHeight });
      if (input.srcObject) {
        frames.push({ stream: "input", mediaTimeMs: input.currentTime * 1000, arrivalTimeMs: performance.now(), width: input.videoWidth, height: input.videoHeight });
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
      audio: { publish: Boolean(inputStream?.getAudioTracks().length), subscribe: remoteStream.getAudioTracks().length > 0 },
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
