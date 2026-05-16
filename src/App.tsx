import { useCallback, useEffect, useReducer, useRef } from "react";
import { listen } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";
import { getCurrentWindow } from "@tauri-apps/api/window";

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

// Per-bar amplitude multipliers — shapes the EQ display profile
const BAR_MULTIPLIERS = [0.45, 0.70, 0.95, 1.0, 0.90, 0.65, 0.42];
const NUM_BARS = BAR_MULTIPLIERS.length;
const MIN_BAR_H = 3;  // px, minimum bar height (silence)
const MAX_BAR_H = 22; // px, additional height at full amplitude

function WaveformBars({
  barsRef,
}: {
  barsRef: React.MutableRefObject<Array<HTMLDivElement | null>>;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 3, height: 28 }}>
      {Array.from({ length: NUM_BARS }, (_, i) => (
        <div
          key={i}
          ref={(el) => {
            barsRef.current[i] = el;
          }}
          style={{
            width: 3,
            height: MIN_BAR_H,
            background: "#60a5fa",
            borderRadius: 2,
            // No CSS animation — RAF drives height directly for real amplitude
          }}
        />
      ))}
    </div>
  );
}

// ── Pill overlay card ──────────────────────────────────────────────────────────

const OVERLAY_STYLES = `
  @keyframes vox-spin {
    to { transform: rotate(360deg); }
  }
  @keyframes vox-blink {
    0%, 49% { opacity: 1; }
    50%, 100% { opacity: 0; }
  }
  @keyframes vox-appear {
    from { opacity: 0; transform: scale(0.92) translateY(6px); }
    to   { opacity: 1; transform: scale(1)    translateY(0);   }
  }
`;

function Spinner() {
  return (
    <div
      style={{
        width: 15,
        height: 15,
        border: "2px solid rgba(255,255,255,0.12)",
        borderTopColor: "#60a5fa",
        borderRadius: "50%",
        animation: "vox-spin 0.75s linear infinite",
        flexShrink: 0,
      }}
    />
  );
}

const PILL: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 12,
  padding: "0 20px",
  height: 56,
  background: "rgba(10, 10, 10, 0.93)",
  borderRadius: 28,
  border: "1px solid rgba(255,255,255,0.07)",
  boxShadow: "0 8px 32px rgba(0,0,0,0.55), inset 0 1px 0 rgba(255,255,255,0.05)",
  color: "#fff",
  fontFamily: "system-ui, -apple-system, sans-serif",
  fontSize: 14,
  fontWeight: 400,
  whiteSpace: "nowrap",
  animation: "vox-appear 120ms ease-out both",
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
      <div style={PILL}>
        <div style={{ width: 8, height: 8, borderRadius: "50%", background: "#fbbf24", flexShrink: 0 }} />
        <span style={{ color: "#94a3b8" }}>Vox is starting...</span>
      </div>
    );
  }

  if (state.status === "degraded") {
    return (
      <div style={{ ...PILL, borderColor: "rgba(239,68,68,0.25)" }}>
        <span style={{ color: "#f87171" }}>Degraded</span>
      </div>
    );
  }

  if (state.status === "recording") {
    return (
      <div style={PILL}>
        <WaveformBars barsRef={barsRef} />
        <span style={{ color: "#94a3b8" }}>Listening...</span>
      </div>
    );
  }

  if (state.status === "streaming") {
    return (
      <div style={{ ...PILL, maxWidth: 380 }}>
        <WaveformBars barsRef={barsRef} />
        <span style={{ color: "#f1f5f9", overflow: "hidden", textOverflow: "ellipsis" }}>
          {state.partial || "..."}
          <span
            style={{
              display: "inline-block",
              width: 2,
              height: "1em",
              background: "#60a5fa",
              marginLeft: 2,
              verticalAlign: "text-bottom",
              animation: "vox-blink 1s step-end infinite",
            }}
          />
        </span>
      </div>
    );
  }

  if (state.status === "finalizing") {
    return (
      <div style={PILL}>
        <Spinner />
        <span style={{ color: "#94a3b8" }}>Processing...</span>
      </div>
    );
  }

  if (state.status === "injecting") {
    return (
      <div style={PILL}>
        <span style={{ color: "#34d399", fontSize: 16, lineHeight: 1 }}>✓</span>
        <span style={{ color: "#94a3b8" }}>Done</span>
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div style={{ ...PILL, borderColor: "rgba(239,68,68,0.25)" }}>
        <span style={{ color: "#f87171" }}>{state.message}</span>
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

// ── Window helpers ────────────────────────────────────────────────────────────
// Show is handled by Rust (hotkey handler calls w.show() before emitting event).
// Hide goes through a Tauri command — no frontend capability permission needed.

function showWindow() {
  getCurrentWindow().show()
    .then(() => { getCurrentWindow().setFocus(); })
    .then(() => console.log("[vox] window: shown"))
    .catch((e) => console.log("[vox] window: show failed:", e));
}

function hideWindow() {
  invoke("hide_main_window")
    .then(() => console.log("[vox] window: hidden"))
    .catch((e) => console.log("[vox] window: hide failed:", e));
}

// ── App ───────────────────────────────────────────────────────────────────────

export default function App() {
  const [state, dispatch] = useReducer(reducer, { status: "waiting_for_models" });
  const stateRef = useRef(state);
  useEffect(() => { stateRef.current = state; }, [state]);

  // Tracks whether the hotkey is currently held — prevents hide effect from
  // closing the window mid-hold if models-ready fires while key is down
  const hotkeyHeldRef = useRef(false);

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
          hotkeyHeldRef.current = true;
          const s = stateRef.current.status;
          console.log(`[vox] event: hotkey-pressed (state=${s})`);
          if (s === "recording" || s === "streaming") {
            console.log("[vox] hotkey-pressed: cancelling");
            cancelStream();
            dispatch({ type: "CANCEL_RECORDING" });
            hideWindow();
          } else if (s === "idle") {
            console.log("[vox] hotkey-pressed: starting recording");
            levelRef.current = 0;
            lastLevelTimeRef.current = 0;
            smoothedRef.current = 0;
            showWindow();
            beginStream();
            dispatch({ type: "START_RECORDING" });
          } else {
            // waiting_for_models / degraded — show feedback pill
            console.log(`[vox] hotkey-pressed: not ready (state=${s}), showing feedback pill`);
            showWindow();
          }
        })
      );

      cleanups.push(
        await listen<null>("hotkey-released", () => {
          hotkeyHeldRef.current = false;
          const s = stateRef.current.status;
          console.log(`[vox] event: hotkey-released (state=${s})`);
          if (s === "recording" || s === "streaming") {
            console.log("[vox] hotkey-released: terminating stream");
            terminateStream();
            dispatch({ type: "STOP_RECORDING" });
            hideWindow();
          } else if (s === "waiting_for_models" || s === "degraded") {
            console.log("[vox] hotkey-released: hiding feedback pill");
            hideWindow();
          } else {
            console.log(`[vox] hotkey-released: ignored (state=${s})`);
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

  // ERROR auto-reset after 4s
  useEffect(() => {
    if (state.status === "error") {
      const id = setTimeout(() => dispatch({ type: "RESET" }), 4000);
      return () => clearTimeout(id);
    }
  }, [state.status]);

  // Hide window when leaving active states, unless hotkey is still held
  useEffect(() => {
    const visibleStates = ["recording", "streaming"];
    if (!visibleStates.includes(state.status) && !hotkeyHeldRef.current) {
      hideWindow();
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
