import { useCallback, useEffect, useReducer, useRef } from "react";
import { listen } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";

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

// ── Port discovery ─────────────────────────────────────────────────────────────

const DEFAULT_PORT = 8000;
let sidecarPort = DEFAULT_PORT;

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
  console.log(`[vox] port-discovery: no sidecar found, defaulting to ${DEFAULT_PORT}`);
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
      if (state.status === "waiting_for_models") return { status: "idle" };
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
const BAR_MULTIPLIERS = [0.22, 0.46, 0.72, 0.96, 0.82, 0.58, 0.78, 0.40, 0.25, 0.52, 0.88];
const NUM_BARS = BAR_MULTIPLIERS.length;
const MIN_BAR_H = 7;
const MAX_BAR_H = 16;

function WaveformBars({
  barsRef,
}: {
  barsRef: React.MutableRefObject<Array<HTMLDivElement | null>>;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 3, height: 26, flex: "1 1 auto", justifyContent: "center", minWidth: 84 }}>
      {Array.from({ length: NUM_BARS }, (_, i) => (
        <div
          key={i}
          ref={(el) => { barsRef.current[i] = el; }}
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
  background: "rgba(255, 255, 255, 0.82)",
  borderRadius: 999,
  border: "1px solid rgba(43, 54, 62, 0.12)",
  boxShadow: "0 10px 22px rgba(41, 52, 61, 0.18)",
  color: "#1d2329",
  fontFamily: "system-ui, -apple-system, sans-serif",
  fontSize: 13,
  backdropFilter: "blur(18px)",
  whiteSpace: "nowrap",
};

const PEARL_PILL: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 8,
  padding: "0 16px",
  height: 36,
  background: "rgba(255, 255, 255, 0.82)",
  borderRadius: 999,
  border: "1px solid rgba(43, 54, 62, 0.12)",
  boxShadow: "0 5px 12px rgba(41, 52, 61, 0.12)",
  color: "#1d2329",
  fontFamily: "system-ui, -apple-system, sans-serif",
  fontSize: 13,
  backdropFilter: "blur(16px)",
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
        <div style={{ width: 7, height: 7, borderRadius: "50%", background: "#d97706", flexShrink: 0 }} />
        <span style={{ color: "#5a6470" }}>Starting…</span>
      </div>
    );
  }

  if (state.status === "degraded") {
    return (
      <div style={{ ...PEARL_PILL, borderColor: "rgba(239, 68, 68, 0.25)" }}>
        <div style={{ width: 7, height: 7, borderRadius: "50%", background: "#ef4444", flexShrink: 0 }} />
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
        <div className="vox-icon-check" style={{ opacity: 0.35 }} />
      </div>
    );
  }

  if (state.status === "finalizing") {
    return (
      <div className="vox-capsule" style={CAPSULE}>
        <div className="vox-icon-x" style={{ opacity: 0.3 }} />
        <div style={{ flex: "1 1 auto", display: "flex", alignItems: "center", justifyContent: "center", gap: 7, minWidth: 84 }}>
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
        <div style={{ flex: "1 1 auto", display: "flex", alignItems: "center", justifyContent: "center", minWidth: 84 }}>
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
  lastLevelTimeRef: React.MutableRefObject<number>
) {
  const wsRef = useRef<WebSocket | null>(null);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout>>();
  const retryDelayRef = useRef(500);

  const connect = useCallback(() => {
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
        return; // high-frequency, no logging
      }

      console.log("[vox] ws: message received", msg.type, msg);

      if (msg.type === "partial_update" && msg.content) {
        dispatch({ type: "PARTIAL_UPDATE", content: msg.content });
      } else if (msg.type === "handoff_ready" && msg.canary_transcript) {
        console.log("[vox] ws: handoff_ready transcript:", msg.canary_transcript);
        dispatch({ type: "HANDOFF_READY", canaryTranscript: msg.canary_transcript });
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
      const delay = Math.min(30000, retryDelayRef.current);
      console.log(`[vox] ws: closed (code=${e.code}), reconnecting in ${delay}ms`);
      retryDelayRef.current = delay * 2;
      retryTimerRef.current = setTimeout(connect, delay);
    };
  }, [dispatch, onHandoff, levelRef, lastLevelTimeRef]);

  useEffect(() => {
    connect();
    return () => {
      wsRef.current?.close();
      clearTimeout(retryTimerRef.current);
    };
  }, [connect]);

  const sendWs = useCallback((msg: string) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(msg);
    } else {
      console.log("[vox] ws: not open (readyState=" + ws?.readyState + "), message dropped:", msg);
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

  return { wsRef, beginStream, terminateStream, cancelStream };
}

// Window show/hide is managed entirely by Rust:
//   hotkey PRESSED  → Rust calls w.show() + w.set_focus() before emitting event
//   hotkey RELEASED → Rust calls w.hide() before emitting event
// React never touches window visibility — eliminates async JS/Rust show-hide race.

// ── App ───────────────────────────────────────────────────────────────────────

export default function App() {
  const [state, dispatch] = useReducer(reducer, { status: "waiting_for_models" });
  const stateRef = useRef(state);
  useEffect(() => { stateRef.current = state; }, [state]);

  // Refs for real-time audio level animation (no React re-renders)
  const barsRef = useRef<Array<HTMLDivElement | null>>(new Array(NUM_BARS).fill(null));
  const levelRef = useRef(0);
  const lastLevelTimeRef = useRef(0);
  const smoothedRef = useRef(0);
  const rafRef = useRef<number>();

  // RAF loop: runs at 60fps, drives bar heights from real audio amplitude
  useEffect(() => {
    const animate = () => {
      // If no audio_level received in the last 400ms, use a slow idle pulse as fallback
      const hasRealLevel = Date.now() - lastLevelTimeRef.current < 400;
      const target = hasRealLevel
        ? levelRef.current
        : Math.abs(Math.sin(Date.now() / 700)) * 0.06; // subtle idle breathing

      // Exponential smoothing: fast attack (~4 frames), slower decay (~12 frames)
      smoothedRef.current = smoothedRef.current * 0.75 + target * 0.25;
      const s = smoothedRef.current;

      barsRef.current.forEach((bar, i) => {
        if (bar) {
          // Small per-frame noise adds organic feel; scaled by current level
          const noise = (Math.random() - 0.5) * s * 2;
          const h = MIN_BAR_H + (s * MAX_BAR_H + noise) * BAR_MULTIPLIERS[i];
          bar.style.height = `${Math.max(MIN_BAR_H, h)}px`;
        }
      });

      rafRef.current = requestAnimationFrame(animate);
    };

    rafRef.current = requestAnimationFrame(animate);
    return () => { if (rafRef.current) cancelAnimationFrame(rafRef.current); };
  }, []);

  // M9 injection stub — M10 replaces with fetch /process-text + invoke inject_text
  const handleHandoff = useCallback((_canaryTranscript: string) => {
    console.log("[vox] handleHandoff stub: dispatching INJECTION_DONE (M10 will inject)");
    setTimeout(() => dispatch({ type: "INJECTION_DONE" }), 300);
  }, []);

  const { beginStream, terminateStream, cancelStream } = useWebSocket(
    dispatch,
    handleHandoff,
    levelRef,
    lastLevelTimeRef
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
            levelRef.current = 0;
            lastLevelTimeRef.current = 0;
            smoothedRef.current = 0;
            beginStream();
            dispatch({ type: "START_RECORDING" });
          } else {
            // waiting_for_models / degraded — state content is already rendered; move on-screen
            invoke("position_overlay").catch(console.error);
            console.log(`[vox] hotkey-pressed: not ready (state=${s})`);
          }
        })
      );

      cleanups.push(
        await listen<null>("hotkey-released", () => {
          const s = stateRef.current.status;
          console.log(`[vox] event: hotkey-released (state=${s})`);
          if (s === "recording" || s === "streaming") {
            console.log("[vox] hotkey-released: terminating stream");
            terminateStream();
            dispatch({ type: "STOP_RECORDING" });
          } else {
            console.log(`[vox] hotkey-released: no stream to stop (state=${s})`);
          }
        })
      );

      cleanups.push(
        await listen<null>("models-ready", async () => {
          console.log("[vox] event: models-ready — discovering port...");
          sidecarPort = await discoverSidecarPort();
          dispatch({ type: "MODELS_READY" });
        })
      );

      cleanups.push(
        await listen<{ missing: string[] }>("sidecar-degraded", (e) => {
          console.log("[vox] event: sidecar-degraded, missing=", e.payload.missing);
          dispatch({ type: "MODELS_DEGRADED", missing: e.payload.missing });
          // Do NOT showWindow() — window only appears on hotkey press
        })
      );

      cleanups.push(
        await listen<null>("sidecar-restarting", async () => {
          console.log("[vox] event: sidecar-restarting, re-discovering port...");
          dispatch({ type: "SIDECAR_RESTARTING" });
          sidecarPort = await discoverSidecarPort();
        })
      );
    };

    reg().then(async () => {
      // Race-condition guard: models-ready may have fired before listeners registered
      const port = await checkAlreadyReady();
      if (port !== null && stateRef.current.status === "waiting_for_models") {
        console.log(`[vox] post-register: sidecar already ready on port ${port}`);
        sidecarPort = port;
        dispatch({ type: "MODELS_READY" });
      }
    });

    return () => cleanups.forEach((fn) => fn());
  }, [beginStream, cancelStream, terminateStream]);

  // Move window on-screen after React has rendered the pill (useEffect fires post-paint).
  // Only fires for "recording" — streaming keeps it on-screen without re-invoking,
  // and finalizing/injecting must stay off-screen to avoid fighting Rust's RELEASED handler.
  useEffect(() => {
    if (state.status === "recording") {
      invoke("position_overlay").catch(console.error);
    }
  }, [state.status]);

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
