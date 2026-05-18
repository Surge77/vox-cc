import { useCallback, useEffect, useReducer, useRef } from "react";
import { listen } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";
import { getCurrentWindow, LogicalPosition } from "@tauri-apps/api/window";
import StreamingOverlay from "./StreamingOverlay";

// ── Types ─────────────────────────────────────────────────────────────────────

type AppState =
  | { status: "waiting_for_models" }
  | { status: "degraded"; missing: string[] }
  | { status: "idle" }
  | { status: "capturing" }
  | { status: "recording" }
  | { status: "streaming"; partial: string }
  | { status: "finalizing" }
  | { status: "injecting" }
  | { status: "error"; message: string };

type AppAction =
  | { type: "MODELS_READY" }
  | { type: "MODELS_DEGRADED"; missing: string[] }
  | { type: "START_RECORDING" }
  | { type: "STREAM_STARTED" }
  | { type: "STREAM_STOPPED" }
  | { type: "STOP_RECORDING" }
  | { type: "CANCEL_RECORDING" }
  | { type: "PARTIAL_UPDATE"; content: string }
  | { type: "HANDOFF_READY"; canaryTranscript: string }
  | { type: "INJECTION_DONE" }
  | { type: "SIDECAR_RESTARTING" }
  | { type: "ERROR"; message: string }
  | { type: "RESET" };

interface WsMessage {
  type:
    | "partial_update"
    | "handoff_ready"
    | "error"
    | "audio_level"
    | "stream_started"
    | "stream_stopped";
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
  style?: string;
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
      if (state.status === "idle") return { status: "capturing" };
      return state;
    case "STREAM_STARTED":
      if (state.status === "capturing") return { status: "recording" };
      return state;
    case "STREAM_STOPPED":
      if (state.status === "recording" || state.status === "streaming")
        return { status: "finalizing" };
      return state;
    case "STOP_RECORDING":
      if (
        state.status === "capturing" ||
        state.status === "recording" ||
        state.status === "streaming"
      )
        return { status: "finalizing" };
      return state;
    case "CANCEL_RECORDING":
      if (
        state.status === "capturing" ||
        state.status === "recording" ||
        state.status === "streaming"
      )
        return { status: "idle" };
      return state;
    case "PARTIAL_UPDATE":
      if (
        state.status === "capturing" ||
        state.status === "recording" ||
        state.status === "streaming"
      )
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
  // Fallback: if sidecar doesn't send stream_started, first audio_level promotes
  // capturing → recording so the capsule isn't stuck invisible.
  const streamStartedRef = useRef(false);

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
        // Fallback for older sidecar builds that don't send stream_started:
        // first audio_level proves capture is active — promote capturing → recording.
        if (!streamStartedRef.current) {
          streamStartedRef.current = true;
          dispatch({ type: "STREAM_STARTED" });
        }
        return; // high-frequency, no logging
      }

      if (msg.type === "stream_started") {
        dispatch({ type: "STREAM_STARTED" });
        return;
      }
      if (msg.type === "stream_stopped") {
        dispatch({ type: "STREAM_STOPPED" });
        return;
      }

      console.log("[vox] ws: message received", msg.type, msg);

      if (msg.type === "partial_update" && msg.content) {
        dispatch({ type: "PARTIAL_UPDATE", content: msg.content });
      } else if (msg.type === "handoff_ready") {
        if (msg.canary_transcript) {
          console.log(
            "[vox] ws: handoff_ready transcript:",
            msg.canary_transcript,
          );
          dispatch({
            type: "HANDOFF_READY",
            canaryTranscript: msg.canary_transcript,
          });
          onHandoff(msg.canary_transcript);
        } else {
          console.log("[vox] ws: handoff_ready empty — resetting to idle");
          dispatch({ type: "CANCEL_RECORDING" });
        }
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
    streamStartedRef.current = false; // reset per-session so fallback fires again
    let deviceIndex: number | null = null;
    try {
      const raw = localStorage.getItem("vox_settings");
      if (raw) {
        const s = JSON.parse(raw) as { audioDeviceIndex?: unknown };
        if (typeof s.audioDeviceIndex === "number")
          deviceIndex = s.audioDeviceIndex;
      }
    } catch {
      // ignore — use sidecar default
    }
    console.log("[vox] ws: sending begin_stream", { deviceIndex });
    sendWs(
      JSON.stringify({ command: "begin_stream", device_index: deviceIndex }),
    );
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

  // Refs for real-time audio level — passed to StreamingOverlay for RAF animation
  const levelRef = useRef(0);
  const lastLevelTimeRef = useRef(0);
  const contextRef = useRef<DeepContextPayload | null>(null);
  const speechDetectedRef = useRef(false);
  const recordingStartTimeRef = useRef(0);

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
      const storedSettings = (() => {
        try {
          return JSON.parse(localStorage.getItem("vox_settings") ?? "{}") as {
            defaultStyle?: string;
            useLlm?: boolean;
          };
        } catch {
          return {};
        }
      })();
      const body: ProcessTextRequest = {
        raw_transcript: canaryTranscript,
        context_string: ctx?.window_title ?? "",
        executable_name: ctx?.executable_name ?? "",
        window_title: ctx?.window_title ?? "",
        inferred_extension: ctx?.inferred_extension ?? null,
        text_preceding_cursor: ctx?.text_preceding_cursor ?? "",
        text_succeeding_cursor: ctx?.text_succeeding_cursor ?? "",
        use_local_llm: storedSettings.useLlm ?? true,
        custom_vocabulary: [],
        style: storedSettings.defaultStyle ?? "auto",
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
          if (s === "capturing" || s === "recording" || s === "streaming") {
            console.log("[vox] hotkey-pressed: cancelling");
            cancelStream();
            dispatch({ type: "CANCEL_RECORDING" });
          } else if (s === "idle") {
            console.log("[vox] hotkey-pressed: starting recording");
            // Rust already moved+showed the window synchronously before this event
            // arrived. Call position_overlay again as a React-side confirmation so
            // any failure is visible in DevTools rather than silently swallowed.
            invoke("position_overlay").catch((e: unknown) => {
              console.error("[vox] position_overlay failed:", e);
            });
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
          if (s === "capturing" || s === "recording" || s === "streaming") {
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
      if (
        port !== null &&
        (stateRef.current.status === "waiting_for_models" ||
          stateRef.current.status === "degraded")
      ) {
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

  // Health poll while waiting_for_models — handles missed models-ready events.
  // Sidecar may be ready before listeners registered; poll until confirmed.
  useEffect(() => {
    if (state.status !== "waiting_for_models") return;
    let cancelled = false;
    const poll = async () => {
      if (cancelled) return;
      const port = await checkAlreadyReady();
      if (port !== null && !cancelled) {
        console.log(`[vox] health-poll: sidecar ready on port ${port}`);
        sidecarPort = port;
        forceReconnect();
        dispatch({ type: "MODELS_READY" });
      }
    };
    poll();
    const id = setInterval(poll, 2000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [state.status, forceReconnect]);

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
    <StreamingOverlay
      status={state.status}
      partial={state.status === "streaming" ? state.partial : ""}
      degraded={state.status === "degraded" ? state.missing : []}
      levelRef={levelRef}
      lastLevelTimeRef={lastLevelTimeRef}
    />
  );
}
