import { useCallback, useEffect, useReducer, useRef } from "react";
import { listen } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";
import { getCurrentWindow, LogicalPosition } from "@tauri-apps/api/window";

// ── Types ─────────────────────────────────────────────────────────────────────

type AppState =
  | { status: "waiting_for_models" }
  | { status: "degraded"; missing: string[] }
  | { status: "idle" }
  | { status: "recording" }
  | { status: "streaming"; partial: string }
  | { status: "finalizing" }
  | { status: "injecting" }
  | { status: "error"; message: string };

type AppAction =
  | { type: "MODELS_READY" }
  | { type: "MODELS_DEGRADED"; missing: string[] }
  | { type: "START_RECORDING" }
  | { type: "STOP_RECORDING" }
  | { type: "CANCEL_RECORDING" }
  | { type: "PARTIAL_UPDATE"; content: string }
  | { type: "HANDOFF_READY"; canaryTranscript: string }
  | { type: "INJECTION_DONE" }
  | { type: "SIDECAR_RESTARTING" }
  | { type: "ERROR"; message: string }
  | { type: "RESET" };

interface WsMessage {
  type: "partial_update" | "handoff_ready" | "error" | "audio_level";
  content?: string;
  canary_transcript?: string;
  message?: string;
  level?: number;
}

interface DeepContextPayload {
  executable_name: string;
  window_title: string;
  inferred_extension: string | null;
  text_preceding_cursor: string;
  text_succeeding_cursor: string;
}

interface ProcessTextRequest {
  raw_transcript: string;
  context_string: string;
  executable_name: string;
  window_title: string;
  inferred_extension: string | null;
  text_preceding_cursor: string;
  text_succeeding_cursor: string;
  use_local_llm: boolean;
  custom_vocabulary: string[];
}

// ── Port discovery ─────────────────────────────────────────────────────────────

const DEFAULT_PORT = 8000;
let sidecarPort = 0; // deferred — set by sidecar-port event before WS connects

async function discoverSidecarPort(): Promise<number> {
  console.log("[vox] port-discovery: scanning 8000-8009...");
  for (let port = 8000; port <= 8009; port++) {
    try {
      const resp = await fetch(`http://127.0.0.1:${port}/health`, {
        signal: AbortSignal.timeout(500),
      });
      if (resp.ok) {
        console.log(`[vox] port-discovery: found sidecar on port ${port}`);
        return port;
      }
    } catch {
      continue;
    }
  }
  console.log(
    `[vox] port-discovery: no sidecar found, defaulting to ${DEFAULT_PORT}`,
  );
  return DEFAULT_PORT;
}

async function checkAlreadyReady(): Promise<number | null> {
  for (let port = 8000; port <= 8009; port++) {
    try {
      const resp = await fetch(`http://127.0.0.1:${port}/health`, {
        signal: AbortSignal.timeout(500),
      });
      if (resp.ok) {
        const body = await resp.json();
        if (body.status === "ok") return port;
      }
    } catch {
      continue;
    }
  }
  return null;
}

const WS_URL = () => `ws://127.0.0.1:${sidecarPort}/ws/dictation`;

// ── Reducer ───────────────────────────────────────────────────────────────────

function reducer(state: AppState, action: AppAction): AppState {
  switch (action.type) {
    case "MODELS_READY":
      if (state.status === "waiting_for_models" || state.status === "degraded")
        return { status: "idle" };
      return state;
    case "MODELS_DEGRADED":
      return { status: "degraded", missing: action.missing };
    case "START_RECORDING":
      if (state.status === "idle") return { status: "recording" };
      return state;
    case "STOP_RECORDING":
      if (state.status === "recording" || state.status === "streaming")
        return { status: "finalizing" };
      return state;
    case "CANCEL_RECORDING":
      if (state.status === "recording" || state.status === "streaming")
        return { status: "idle" };
      return state;
    case "PARTIAL_UPDATE":
      if (state.status === "recording" || state.status === "streaming")
        return { status: "streaming", partial: action.content };
      return state;
    case "HANDOFF_READY":
      if (state.status === "finalizing") return { status: "injecting" };
      return state;
    case "INJECTION_DONE":
      if (state.status === "injecting") return { status: "idle" };
      return state;
    case "SIDECAR_RESTARTING":
      return { status: "waiting_for_models" };
    case "ERROR":
      return { status: "error", message: action.message };
    case "RESET":
      return { status: "idle" };
    default:
      return state;
  }
}

// ── Waveform bars (RAF-animated, real amplitude) ───────────────────────────────

// Pearl Compact (v2) — 11-bar EQ profile matching waveform-loader-capsules.html
const BAR_MULTIPLIERS = [
  0.22, 0.46, 0.72, 0.96, 0.82, 0.58, 0.78, 0.4, 0.25, 0.52, 0.88,
];
// Each bar oscillates at its own period (ms). Prime-ish values prevent synchronisation.
const BAR_SPEEDS = [127, 97, 73, 113, 83, 139, 67, 103, 151, 89, 109];
const NUM_BARS = BAR_MULTIPLIERS.length;
const MIN_BAR_H = 4;
const MAX_BAR_H = 28;

function WaveformBars({
  barsRef,
}: {
  barsRef: React.MutableRefObject<Array<HTMLDivElement | null>>;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 3,
        height: 26,
        flex: "1 1 auto",
        justifyContent: "center",
        minWidth: 84,
      }}
    >
      {Array.from({ length: NUM_BARS }, (_, i) => (
        <div
          key={i}
          ref={(el) => {
            barsRef.current[i] = el;
          }}
          style={{
            width: 3,
            height: MIN_BAR_H,
            background: "linear-gradient(180deg, #111a22, #65707a)",
            borderRadius: 999,
            flexShrink: 0,
          }}
        />
      ))}
    </div>
  );
}

// ── Pearl Compact capsule (v2 from waveform-loader-capsules.html) ──────────────

const OVERLAY_STYLES = `
  @keyframes vox-spin { to { transform: rotate(360deg); } }
  @keyframes vox-capsule-breathe { 50% { transform: translateY(-1px) scale(1.018); } }

  .vox-capsule {
    position: relative;
    isolation: isolate;
    overflow: hidden;
  }
  .vox-capsule::before {
    content: "";
    position: absolute;
    inset: 1px;
    border-radius: 999px;
    background: linear-gradient(180deg, rgba(255,255,255,0.22), transparent 58%);
    pointer-events: none;
    z-index: -1;
  }
  .vox-capsule::after {
    content: "";
    position: absolute;
    inset: auto 13px 6px;
    height: 1px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.76);
    pointer-events: none;
  }
  .vox-capsule-active { animation: vox-capsule-breathe 1.7s ease-in-out infinite; }

  .vox-icon-x {
    position: relative;
    flex-shrink: 0;
    width: 30px;
    height: 30px;
    display: inline-grid;
    place-items: center;
    border: none;
    border-radius: 999px;
    background: rgba(20, 28, 36, 0.07);
    padding: 0;
    cursor: default;
  }
  .vox-icon-x::before,
  .vox-icon-x::after {
    content: "";
    position: absolute;
    width: 11px;
    height: 2px;
    border-radius: 999px;
    background: #5a6470;
  }
  .vox-icon-x::before { transform: rotate(45deg); }
  .vox-icon-x::after  { transform: rotate(-45deg); }

  .vox-icon-check {
    position: relative;
    flex-shrink: 0;
    width: 30px;
    height: 30px;
    display: inline-grid;
    place-items: center;
    border: none;
    border-radius: 999px;
    background: rgba(20, 28, 36, 0.07);
    padding: 0;
    cursor: default;
  }
  .vox-icon-check::before {
    content: "";
    display: block;
    width: 12px;
    height: 7px;
    border-left: 2px solid #1d2329;
    border-bottom: 2px solid #1d2329;
    transform: translateY(-1px) rotate(-45deg);
  }
`;

function Spinner() {
  return (
    <div
      style={{
        width: 15,
        height: 15,
        border: "2px solid rgba(43, 54, 62, 0.15)",
        borderTopColor: "#1d2329",
        borderRadius: "50%",
        animation: "vox-spin 0.75s linear infinite",
        flexShrink: 0,
      }}
    />
  );
}

const CAPSULE: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 9,
  padding: "0 9px",
  height: 50,
  minWidth: 206,
  background: "rgba(255, 255, 255, 0.96)",
  borderRadius: 999,
  border: "1px solid rgba(43, 54, 62, 0.12)",
  boxShadow: "0 4px 14px rgba(41, 52, 61, 0.18)",
  color: "#1d2329",
  fontFamily: "system-ui, -apple-system, sans-serif",
  fontSize: 13,
  whiteSpace: "nowrap",
};

const PEARL_PILL: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 8,
  padding: "0 16px",
  height: 36,
  background: "rgba(255, 255, 255, 0.96)",
  borderRadius: 999,
  border: "1px solid rgba(43, 54, 62, 0.12)",
  boxShadow: "0 4px 12px rgba(41, 52, 61, 0.12)",
  color: "#1d2329",
  fontFamily: "system-ui, -apple-system, sans-serif",
  fontSize: 13,
};

function OverlayCard({
  state,
  barsRef,
}: {
  state: AppState;
  barsRef: React.MutableRefObject<Array<HTMLDivElement | null>>;
}) {
  if (state.status === "idle") return null;

  if (state.status === "waiting_for_models") {
    return (
      <div style={PEARL_PILL}>
        <div
          style={{
            width: 7,
            height: 7,
            borderRadius: "50%",
            background: "#d97706",
            flexShrink: 0,
          }}
        />
        <span style={{ color: "#5a6470" }}>Starting…</span>
      </div>
    );
  }

  if (state.status === "degraded") {
    return (
      <div style={{ ...PEARL_PILL, borderColor: "rgba(239, 68, 68, 0.25)" }}>
        <div
          style={{
            width: 7,
            height: 7,
            borderRadius: "50%",
            background: "#ef4444",
            flexShrink: 0,
          }}
        />
        <span style={{ color: "#b91c1c" }}>Degraded</span>
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div style={{ ...PEARL_PILL, borderColor: "rgba(239, 68, 68, 0.25)" }}>
        <span style={{ color: "#b91c1c" }}>{state.message}</span>
      </div>
    );
  }

  if (state.status === "recording" || state.status === "streaming") {
    return (
      <div className="vox-capsule vox-capsule-active" style={CAPSULE}>
        <div className="vox-icon-x" />
        <WaveformBars barsRef={barsRef} />
        <span
          style={{
            color: "#5a6470",
            fontSize: 12,
            fontWeight: 500,
            flexShrink: 0,
            letterSpacing: "0.01em",
          }}
        >
          Listening…
        </span>
      </div>
    );
  }

  if (state.status === "finalizing") {
    return (
      <div className="vox-capsule" style={CAPSULE}>
        <div className="vox-icon-x" style={{ opacity: 0.3 }} />
        <div
          style={{
            flex: "1 1 auto",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 7,
            minWidth: 84,
          }}
        >
          <Spinner />
          <span style={{ color: "#5a6470" }}>Processing…</span>
        </div>
        <div className="vox-icon-check" style={{ opacity: 0.3 }} />
      </div>
    );
  }

  if (state.status === "injecting") {
    return (
      <div className="vox-capsule" style={CAPSULE}>
        <div className="vox-icon-x" style={{ opacity: 0.3 }} />
        <div
          style={{
            flex: "1 1 auto",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            minWidth: 84,
          }}
        >
          <span style={{ color: "#5a6470" }}>Done</span>
        </div>
        <div className="vox-icon-check" />
      </div>
    );
  }

  return null;
}

// ── useWebSocket ──────────────────────────────────────────────────────────────

function useWebSocket(
  dispatch: React.Dispatch<AppAction>,
  onHandoff: (transcript: string) => void,
  levelRef: React.MutableRefObject<number>,
  lastLevelTimeRef: React.MutableRefObject<number>,
  speechDetectedRef: React.MutableRefObject<boolean>,
) {
  const wsRef = useRef<WebSocket | null>(null);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout>>();
  const retryDelayRef = useRef(500);
  const activeRef = useRef(true);

  const connect = useCallback(() => {
    if (sidecarPort === 0) return; // port not known yet — wait for sidecar-port event
    const url = WS_URL();
    console.log(`[vox] ws: connecting to ${url}`);
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      console.log("[vox] ws: connected");
      retryDelayRef.current = 500;
    };

    ws.onmessage = (event) => {
      let msg: WsMessage;
      try {
        msg = JSON.parse(event.data as string);
      } catch {
        return;
      }

      if (msg.type === "audio_level" && typeof msg.level === "number") {
        levelRef.current = msg.level;
        lastLevelTimeRef.current = Date.now();
        // level = rms * 8 (scaled in dictation.py). Threshold 0.064 = raw RMS 0.008 × 8 scale.
        if (msg.level > 0.064) speechDetectedRef.current = true;
        return; // high-frequency, no logging
      }

      console.log("[vox] ws: message received", msg.type, msg);

      if (msg.type === "partial_update" && msg.content) {
        dispatch({ type: "PARTIAL_UPDATE", content: msg.content });
      } else if (msg.type === "handoff_ready" && msg.canary_transcript) {
        console.log(
          "[vox] ws: handoff_ready transcript:",
          msg.canary_transcript,
        );
        dispatch({
          type: "HANDOFF_READY",
          canaryTranscript: msg.canary_transcript,
        });
        onHandoff(msg.canary_transcript);
      } else if (msg.type === "error") {
        console.log("[vox] ws: sidecar error:", msg.message);
        dispatch({ type: "ERROR", message: msg.message ?? "Sidecar error" });
      }
    };

    ws.onerror = (e) => {
      console.log("[vox] ws: error, closing", e);
      ws.close();
    };

    ws.onclose = (e) => {
      if (!activeRef.current) return; // component unmounted — stop reconnect loop
      const delay = Math.min(30000, retryDelayRef.current);
      console.log(
        `[vox] ws: closed (code=${e.code}), reconnecting in ${delay}ms`,
      );
      retryDelayRef.current = delay * 2;
      retryTimerRef.current = setTimeout(connect, delay);
    };
  }, [dispatch, onHandoff, levelRef, lastLevelTimeRef, speechDetectedRef]);

  useEffect(() => {
    activeRef.current = true;
    connect();
    return () => {
      activeRef.current = false;
      wsRef.current?.close();
      clearTimeout(retryTimerRef.current);
    };
  }, [connect]);

  const forceReconnect = useCallback(() => {
    clearTimeout(retryTimerRef.current);
    retryDelayRef.current = 500;
    const ws = wsRef.current;
    if (ws) {
      ws.onclose = null; // prevent exponential backoff from kicking in
      ws.close();
      wsRef.current = null;
    }
    connect();
  }, [connect]);

  const sendWs = useCallback((msg: string) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(msg);
    } else {
      console.log(
        "[vox] ws: not open (readyState=" +
          ws?.readyState +
          "), message dropped:",
        msg,
      );
    }
  }, []);

  const beginStream = useCallback(() => {
    console.log("[vox] ws: sending begin_stream");
    sendWs(JSON.stringify({ command: "begin_stream" }));
  }, [sendWs]);

  const terminateStream = useCallback(() => {
    console.log("[vox] ws: sending terminate_stream");
    sendWs(JSON.stringify({ command: "terminate_stream" }));
  }, [sendWs]);

  const cancelStream = useCallback(() => {
    console.log("[vox] ws: sending cancel_stream");
    sendWs(JSON.stringify({ command: "cancel_stream" }));
  }, [sendWs]);

  return { wsRef, beginStream, terminateStream, cancelStream, forceReconnect };
}

// ── App ───────────────────────────────────────────────────────────────────────

export default function App() {
  const [state, dispatch] = useReducer(reducer, {
    status: "waiting_for_models",
  });
  const stateRef = useRef(state);
  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  // Refs for real-time audio level animation (no React re-renders)
  const barsRef = useRef<Array<HTMLDivElement | null>>(
    new Array(NUM_BARS).fill(null),
  );
  const levelRef = useRef(0);
  const lastLevelTimeRef = useRef(0);
  const smoothedRef = useRef(0);
  const rafRef = useRef<number>();
  const contextRef = useRef<DeepContextPayload | null>(null);
  const speechDetectedRef = useRef(false);
  const recordingStartTimeRef = useRef(0);

  // RAF loop: runs at 60fps, drives bar heights from real audio amplitude
  useEffect(() => {
    const animate = () => {
      const t = Date.now();
      const hasRealLevel = t - lastLevelTimeRef.current < 400;
      // Speech RMS is typically 0.01–0.1 — amplify 8× so bars use full range on normal speech.
      const raw = hasRealLevel ? levelRef.current * 8 : 0;
      const amplified = Math.min(1.0, raw);

      // Fast attack so bars jump on speech onset; slow decay so they ease down naturally.
      if (amplified > smoothedRef.current) {
        smoothedRef.current = smoothedRef.current * 0.2 + amplified * 0.8;
      } else {
        smoothedRef.current = smoothedRef.current * 0.88 + amplified * 0.12;
      }
      const s = smoothedRef.current;

      // baseActivity keeps bars moving even at silence (0.18 floor = gentle idle)
      // and scales to full range when speaking.
      const baseActivity = 0.18 + s * 0.82;

      barsRef.current.forEach((bar, i) => {
        if (!bar) return;
        const spd = BAR_SPEEDS[i];
        const ph1 = (i / NUM_BARS) * Math.PI * 2;
        const ph2 = ph1 + 2.1; // offset second harmonic so bars look independent

        // Two-frequency compound wave — breaks mechanical regularity
        const osc =
          Math.sin(t / spd + ph1) * 0.65 +
          Math.sin(t / (spd * 1.6) + ph2) * 0.35;
        // osc ∈ [–1, 1] → normalise to [0, 1]
        const norm = (osc + 1) / 2;

        const peak = MIN_BAR_H + BAR_MULTIPLIERS[i] * (MAX_BAR_H - MIN_BAR_H);
        const h = MIN_BAR_H + norm * baseActivity * (peak - MIN_BAR_H);
        bar.style.height = `${Math.max(MIN_BAR_H, h)}px`;
      });

      rafRef.current = requestAnimationFrame(animate);
    };

    rafRef.current = requestAnimationFrame(animate);
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
    };
  }, []);

  const handleHandoff = useCallback(
    async (canaryTranscript: string) => {
      if (!canaryTranscript.trim()) {
        // Final pass returned nothing — sidecar processed silence, skip injection.
        console.log(
          "[vox] handleHandoff: empty transcript, skipping injection",
        );
        contextRef.current = null;
        dispatch({ type: "INJECTION_DONE" });
        return;
      }
      const ctx = contextRef.current;
      const body: ProcessTextRequest = {
        raw_transcript: canaryTranscript,
        context_string: ctx?.window_title ?? "",
        executable_name: ctx?.executable_name ?? "",
        window_title: ctx?.window_title ?? "",
        inferred_extension: ctx?.inferred_extension ?? null,
        text_preceding_cursor: ctx?.text_preceding_cursor ?? "",
        text_succeeding_cursor: ctx?.text_succeeding_cursor ?? "",
        use_local_llm: true,
        custom_vocabulary: [],
      };

      let textToInject = canaryTranscript;
      try {
        const resp = await fetch(
          `http://127.0.0.1:${sidecarPort}/process-text`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
            signal: AbortSignal.timeout(45000),
          },
        );
        if (resp.ok) {
          const data = await resp.json();
          if (data.cleaned_text) textToInject = data.cleaned_text;
          if (data.error)
            console.warn("[vox] process-text non-fatal error:", data.error);
        }
      } catch (e) {
        console.warn("[vox] process-text fetch failed, injecting raw:", e);
      }

      try {
        await invoke("inject_text", { text: textToInject });
        console.log("[vox] inject_text: done");
      } catch (e) {
        console.error("[vox] inject_text failed:", e);
      } finally {
        contextRef.current = null;
        dispatch({ type: "INJECTION_DONE" });
      }
    },
    [dispatch],
  );

  const { beginStream, terminateStream, cancelStream, forceReconnect } =
    useWebSocket(
      dispatch,
      handleHandoff,
      levelRef,
      lastLevelTimeRef,
      speechDetectedRef,
    );

  // Tauri event listeners
  useEffect(() => {
    const cleanups: Array<() => void> = [];

    const reg = async () => {
      cleanups.push(
        await listen<null>("hotkey-pressed", () => {
          const s = stateRef.current.status;
          console.log(`[vox] event: hotkey-pressed (state=${s})`);
          if (s === "recording" || s === "streaming") {
            console.log("[vox] hotkey-pressed: cancelling");
            cancelStream();
            dispatch({ type: "CANCEL_RECORDING" });
          } else if (s === "idle") {
            console.log("[vox] hotkey-pressed: starting recording");
            // Capture focused window context before recording starts — target still has focus
            invoke<DeepContextPayload>("get_focused_context")
              .then((ctx) => {
                contextRef.current = ctx;
              })
              .catch(() => {
                contextRef.current = null;
              });
            levelRef.current = 0;
            lastLevelTimeRef.current = 0;
            smoothedRef.current = 0;
            speechDetectedRef.current = false;
            recordingStartTimeRef.current = Date.now();
            beginStream();
            dispatch({ type: "START_RECORDING" });
          } else {
            // waiting_for_models / degraded — ignore hotkey, models not loaded
            console.log(
              `[vox] hotkey-pressed: not ready, ignoring (state=${s})`,
            );
          }
        }),
      );

      cleanups.push(
        await listen<null>("hotkey-released", () => {
          const s = stateRef.current.status;
          console.log(`[vox] event: hotkey-released (state=${s})`);
          if (s === "recording" || s === "streaming") {
            const elapsed = Date.now() - recordingStartTimeRef.current;
            const partial =
              s === "streaming"
                ? (
                    stateRef.current as {
                      status: "streaming";
                      partial: string;
                    }
                  ).partial.trim()
                : "";
            // Cancel only when ALL THREE conditions hold:
            //   1. No audio_level event exceeded 0.064 scaled level (≈ 0.008 raw RMS)
            //   2. No non-empty partial text from Turbo
            //   3. Held long enough (>1200ms) for at least one Turbo chunk to have returned
            // If ANY condition is false we terminate normally and let the sidecar decide.
            const isSilent =
              !speechDetectedRef.current && !partial && elapsed > 1200;
            if (isSilent) {
              console.log(
                `[vox] hotkey-released: silence — no audio, no partial, elapsed=${elapsed}ms, cancelling`,
              );
              cancelStream();
              dispatch({ type: "CANCEL_RECORDING" });
            } else {
              console.log(
                `[vox] hotkey-released: speech present — audio=${speechDetectedRef.current}, partial="${partial.slice(0, 30)}", elapsed=${elapsed}ms, terminating`,
              );
              terminateStream();
              dispatch({ type: "STOP_RECORDING" });
            }
          } else if (s === "waiting_for_models" || s === "degraded") {
            // Hotkey pressed while models still loading — park window immediately
            getCurrentWindow()
              .setPosition(new LogicalPosition(-10000, -10000))
              .catch(() => {});
          } else {
            console.log(
              `[vox] hotkey-released: no stream to stop (state=${s})`,
            );
          }
        }),
      );

      // Rust emits sidecar-port before models-ready — this is the authoritative port source
      cleanups.push(
        await listen<{ port: number }>("sidecar-port", (e) => {
          console.log(`[vox] event: sidecar-port = ${e.payload.port}`);
          sidecarPort = e.payload.port;
          forceReconnect();
        }),
      );

      cleanups.push(
        await listen<null>("models-ready", async () => {
          console.log("[vox] event: models-ready");
          if (sidecarPort === 0) {
            // sidecar-port event didn't fire yet — fall back to scanning
            sidecarPort = await discoverSidecarPort();
            forceReconnect();
          }
          dispatch({ type: "MODELS_READY" });
        }),
      );

      cleanups.push(
        await listen<{ missing: string[] }>("sidecar-degraded", (e) => {
          console.log(
            "[vox] event: sidecar-degraded, missing=",
            e.payload.missing,
          );
          dispatch({ type: "MODELS_DEGRADED", missing: e.payload.missing });
          // Do NOT showWindow() — window only appears on hotkey press
        }),
      );

      cleanups.push(
        await listen<null>("sidecar-restarting", () => {
          console.log("[vox] event: sidecar-restarting, resetting port...");
          sidecarPort = 0; // reset — sidecar-port event will set the new port on restart
          dispatch({ type: "SIDECAR_RESTARTING" });
        }),
      );
    };

    reg().then(async () => {
      // Race-condition guard: models-ready may have fired before listeners registered
      const port = await checkAlreadyReady();
      if (port !== null && stateRef.current.status === "waiting_for_models") {
        console.log(
          `[vox] post-register: sidecar already ready on port ${port}`,
        );
        sidecarPort = port;
        forceReconnect();
        dispatch({ type: "MODELS_READY" });
      }
    });

    return () => cleanups.forEach((fn) => fn());
  }, [beginStream, cancelStream, terminateStream, forceReconnect]);

  // ERROR auto-reset after 4s
  useEffect(() => {
    if (state.status === "error") {
      const id = setTimeout(() => dispatch({ type: "RESET" }), 4000);
      return () => clearTimeout(id);
    }
  }, [state.status]);

  // Finalizing timeout: if sidecar never sends handoff_ready, reset after 35s
  useEffect(() => {
    if (state.status === "finalizing") {
      const id = setTimeout(() => dispatch({ type: "RESET" }), 35_000);
      return () => clearTimeout(id);
    }
  }, [state.status]);

  useEffect(() => {
    console.log("[vox] state ->", state.status, state);
  }, [state]);

  // Park window off-screen when idle — Rust moves it on-screen on hotkey press.
  // setPosition keeps WebView2 compositor active (no reinit, no black/transparent flash).
  useEffect(() => {
    if (state.status === "idle") {
      getCurrentWindow()
        .setPosition(new LogicalPosition(-10000, -10000))
        .catch(() => {});
    }
  }, [state.status]);

  return (
    <>
      <style>{OVERLAY_STYLES}</style>
      <div
        style={{
          width: "100vw",
          height: "100vh",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background: "transparent",
        }}
      >
        <OverlayCard state={state} barsRef={barsRef} />
      </div>
    </>
  );
}
