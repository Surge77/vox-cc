import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { listen } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";
import { getCurrentWindow, LogicalPosition } from "@tauri-apps/api/window";
import StreamingOverlay from "./StreamingOverlay";

// ── Types ─────────────────────────────────────────────────────────────────────

type AppState =
  | { status: "waiting_for_models" }
  | { status: "degraded"; missing: string[] }
  | { status: "idle" }
  | { status: "recording" }
  | { status: "streaming"; partial: string }
  | { status: "finalizing" }
  | { status: "previewing"; transcript: string }
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
  | { type: "PREVIEW_READY"; transcript: string }
  | { type: "PREVIEW_CANCEL" }
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
    | "stream_stopped"
    | "processing_status"
    | "low_confidence"
    | "device_changed";
  content?: string;
  canary_transcript?: string;
  message?: string;
  level?: number;
  bands?: number[]; // 11 FFT band amplitudes [0,1] for spectrum waveform
  stage?: string;
  score?: number; // distil avg_logprob when low_confidence
  threshold?: number;
  device_index?: number | null;
}

interface DeepContextPayload {
  executable_name: string;
  window_title: string;
  inferred_extension: string | null;
  text_preceding_cursor: string;
  text_succeeding_cursor: string;
  app_url: string | null;
  app_file_path: string | null;
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
  skip_llm_if_simple?: boolean;
  app_url?: string | null;
  app_file_path?: string | null;
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
      // Skip the legacy `capturing` intermediate state — the persistent mic +
      // 3 s pre-roll deque means audio capture is already live at hotkey-press.
      // Showing "Listening…" + bars immediately is accurate, not a lie.
      if (state.status === "idle") return { status: "recording" };
      return state;
    case "STREAM_STARTED":
      // Confirmation no-op now — state already advanced to recording.
      return state;
    case "STREAM_STOPPED":
      if (state.status === "recording" || state.status === "streaming")
        return { status: "finalizing" };
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
    case "PREVIEW_READY":
      // Reachable from injecting (after /process-text fetch) when the
      // previewBeforeInject toggle is on.
      if (state.status === "injecting")
        return { status: "previewing", transcript: action.transcript };
      return state;
    case "PREVIEW_CANCEL":
      // Esc during preview → cancel inject entirely
      if (state.status === "previewing") return { status: "idle" };
      return state;
    case "INJECTION_DONE":
      if (state.status === "injecting" || state.status === "previewing")
        return { status: "idle" };
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
  onProcessingStatus: (msg: string) => void,
  levelRef: React.MutableRefObject<number>,
  lastLevelTimeRef: React.MutableRefObject<number>,
  speechDetectedRef: React.MutableRefObject<boolean>,
  bandsRef: React.MutableRefObject<number[]>,
) {
  const wsRef = useRef<WebSocket | null>(null);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout>>();
  const retryDelayRef = useRef(500);
  const activeRef = useRef(true);

  const connect = useCallback(() => {
    if (sidecarPort === 0) {
      console.log(
        "[vox] ws: connect() skipped — sidecarPort=0, waiting for sidecar-port event",
      );
      return;
    }
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
        if (Array.isArray(msg.bands) && msg.bands.length === 11) {
          bandsRef.current = msg.bands as number[];
        }
        // Speech-detected gates the no-speech short-circuit in hotkey-released.
        // Idle meter runs 30 Hz at all times; only mark detected during sessions
        // (reducer ignores STREAM_STARTED now — recording state set on press).
        if (msg.level > 0.064) speechDetectedRef.current = true;
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

      if (msg.type === "processing_status") {
        onProcessingStatus(msg.message ?? "");
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
          dispatch({ type: "RESET" });
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
  }, [
    dispatch,
    onHandoff,
    onProcessingStatus,
    levelRef,
    lastLevelTimeRef,
    speechDetectedRef,
  ]);

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
    let deviceIndex: number | null = null;
    try {
      const raw = localStorage.getItem("vox_settings");
      if (raw) {
        const s = JSON.parse(raw) as { audioDeviceIndex?: unknown };
        if (typeof s.audioDeviceIndex === "number" && s.audioDeviceIndex >= 0)
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
  const bandsRef = useRef<number[]>(new Array(11).fill(0)); // 11 FFT band amplitudes
  const contextRef = useRef<DeepContextPayload | null>(null);
  const speechDetectedRef = useRef(false);
  const recordingStartTimeRef = useRef(0);
  // Continuous mode (Tier 2 #10) — once the user enters continuous on a
  // hotkey-press, this stays true until they press the hotkey again (toggle)
  // or cancel. After each successful inject, if true, recording restarts.
  const continuousActiveRef = useRef(false);

  const handleHandoff = useCallback(
    async (canaryTranscript: string) => {
      console.log(
        "[vox] handleHandoff: entry canary_len=",
        canaryTranscript.length,
        "preview=",
        canaryTranscript.slice(0, 60),
      );
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
            skipLlmIfSimple?: boolean;
            streamingInject?: boolean;
            previewBeforeInject?: boolean;
            continuousMode?: boolean;
            autoStopEnabled?: boolean;
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
        skip_llm_if_simple: storedSettings.skipLlmIfSimple ?? true,
        app_url: ctx?.app_url ?? null,
        app_file_path: ctx?.app_file_path ?? null,
      };

      let elevationError = false;

      if (storedSettings.streamingInject && body.use_local_llm !== false) {
        // SSE path: stream LLM deltas, type each as it arrives. Bypasses
        // clipboard entirely; total length tracked for undo-state recording.
        let totalLen = 0;
        try {
          await invoke("begin_stream_inject");
        } catch (e) {
          console.error("[vox] begin_stream_inject failed:", e);
        }
        try {
          const resp = await fetch(
            `http://127.0.0.1:${sidecarPort}/process-text/stream`,
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body),
              signal: AbortSignal.timeout(60_000),
            },
          );
          if (!resp.ok || !resp.body) {
            throw new Error(`stream HTTP ${resp.status}`);
          }
          const reader = resp.body.getReader();
          const decoder = new TextDecoder("utf-8");
          let buffer = "";
          // SSE events: blank-line-separated; each block has `event:` + `data:`.
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let blankIdx = buffer.indexOf("\n\n");
            while (blankIdx !== -1) {
              const block = buffer.slice(0, blankIdx);
              buffer = buffer.slice(blankIdx + 2);
              const lines = block.split("\n");
              let event = "message";
              let data = "";
              for (const ln of lines) {
                if (ln.startsWith("event:")) event = ln.slice(6).trim();
                else if (ln.startsWith("data:")) data += ln.slice(5).trim();
              }
              if (event === "delta" && data) {
                try {
                  const parsed = JSON.parse(data) as { text?: string };
                  if (parsed.text) {
                    totalLen += parsed.text.length;
                    try {
                      await invoke("inject_text_chunk", { text: parsed.text });
                    } catch (err) {
                      const m = String(err);
                      if (m.includes("elevation_mismatch")) {
                        elevationError = true;
                        dispatch({
                          type: "ERROR",
                          message: "Elevated app — run Vox as Administrator",
                        });
                        return;
                      }
                      console.error("[vox] inject_text_chunk failed:", err);
                    }
                  }
                } catch {
                  // malformed JSON in delta — skip
                }
              }
              blankIdx = buffer.indexOf("\n\n");
            }
          }
        } catch (e) {
          // Fall through to non-streamed fallback so user still gets text.
          console.warn(
            "[vox] streaming inject failed, falling back to clipboard:",
            e,
          );
          try {
            await invoke("inject_text", { text: canaryTranscript });
          } catch (err) {
            console.error("[vox] fallback inject_text failed:", err);
          }
        } finally {
          try {
            await invoke("end_stream_inject", { totalLen });
          } catch (e) {
            console.error("[vox] end_stream_inject failed:", e);
          }
          contextRef.current = null;
          if (!elevationError) {
            dispatch({ type: "INJECTION_DONE" });
          }
        }
        return;
      }

      // Non-streaming path: single round-trip then clipboard inject.
      let textToInject = canaryTranscript;
      try {
        console.log(
          "[vox] handleHandoff: POST /process-text exe=",
          body.executable_name,
        );
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
          // Tier 2 #6 — voice command short-circuit: invoke the keypress
          // command and return without injecting text.
          if (data.command_key) {
            console.log("[vox] voice command:", data.command_key);
            try {
              await invoke("inject_keypress", { combo: data.command_key });
            } catch (err) {
              console.error("[vox] inject_keypress failed:", err);
            }
            contextRef.current = null;
            dispatch({ type: "INJECTION_DONE" });
            return;
          }
          console.log(
            "[vox] handleHandoff: cleaned_len=",
            data.cleaned_text?.length ?? 0,
            "err=",
            data.error ?? null,
          );
          if (data.error)
            console.warn("[vox] process-text non-fatal error:", data.error);
        }
      } catch (e) {
        console.warn("[vox] process-text fetch failed, injecting raw:", e);
      }

      // Optional preview before inject: show the cleaned transcript in the
      // capsule for ~500 ms. User can press Esc during that window to abort
      // the inject. PREVIEW_CANCEL flips state to idle; the inject below
      // checks stateRef.current.status to skip.
      if (storedSettings.previewBeforeInject) {
        dispatch({ type: "PREVIEW_READY", transcript: textToInject });
        await new Promise((resolve) => setTimeout(resolve, 500));
        if (stateRef.current.status !== "previewing") {
          // User pressed Esc — preview was cancelled
          console.log("[vox] preview: cancelled by user, skipping inject");
          contextRef.current = null;
          return;
        }
      }

      try {
        console.log(
          "[vox] handleHandoff: invoking inject_text text_len=",
          textToInject.length,
        );
        await invoke("inject_text", { text: textToInject });
        console.log("[vox] inject_text: done");
      } catch (e) {
        const msg = String(e);
        if (msg.includes("elevation_mismatch")) {
          elevationError = true;
          dispatch({
            type: "ERROR",
            message: "Elevated app — run Vox as Administrator",
          });
        } else {
          console.error("[vox] inject_text failed:", e);
        }
      } finally {
        contextRef.current = null;
        if (!elevationError) {
          dispatch({ type: "INJECTION_DONE" });
        }
      }
    },
    [dispatch],
  );

  const [processingMsg, setProcessingMsg] = useState("");
  const onProcessingStatus = useCallback((msg: string) => {
    setProcessingMsg(msg);
  }, []);

  const { beginStream, terminateStream, cancelStream, forceReconnect } =
    useWebSocket(
      dispatch,
      handleHandoff,
      onProcessingStatus,
      levelRef,
      lastLevelTimeRef,
      speechDetectedRef,
      bandsRef,
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
            continuousActiveRef.current = false;
            cancelStream();
            dispatch({ type: "CANCEL_RECORDING" });
          } else if (s === "idle") {
            console.log("[vox] hotkey-pressed: starting recording");
            // Read continuous + auto-stop on session start so the toggle
            // can be flipped between sessions without restart.
            const settings = (() => {
              try {
                return JSON.parse(
                  localStorage.getItem("vox_settings") ?? "{}",
                ) as {
                  continuousMode?: boolean;
                  autoStopEnabled?: boolean;
                };
              } catch {
                return {};
              }
            })();
            continuousActiveRef.current = !!(
              settings.continuousMode && settings.autoStopEnabled
            );
            // Rust already moved+showed the window synchronously before this event.
            // Skipping the redundant React-side position_overlay invoke trims
            // ~5-20 ms off the press → "Listening…" transition.
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
          // Auto-stop mode = tap-to-start: hotkey-released is a no-op while
          // recording. Sidecar's silence detector will terminate the session.
          const autoStop = (() => {
            try {
              const raw = localStorage.getItem("vox_settings");
              return raw ? (JSON.parse(raw).autoStopEnabled ?? false) : false;
            } catch {
              return false;
            }
          })();
          if ((s === "recording" || s === "streaming") && !autoStop) {
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
            // No-speech short-circuit: hotkey released without any audio above
            // the speech-level threshold (rms ~> 0.002) — skip final pass + LLM
            // + inject entirely. Saves the ~3-5 s distil pass on this machine.
            if (!speechDetectedRef.current && !partial) {
              console.log(
                `[vox] hotkey-released: no speech detected — cancelling (elapsed=${elapsed}ms)`,
              );
              cancelStream();
              dispatch({ type: "CANCEL_RECORDING" });
            } else {
              console.log(
                `[vox] hotkey-released: terminating — elapsed=${elapsed}ms, speechDetected=${speechDetectedRef.current}, partial="${partial.slice(0, 30)}"`,
              );
              terminateStream();
              dispatch({ type: "STOP_RECORDING" });
            }
          } else if (autoStop && (s === "recording" || s === "streaming")) {
            console.log(
              "[vox] hotkey-released: auto-stop mode — sidecar drives termination",
            );
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
        await listen<null>("continuous-toggle", () => {
          const s = stateRef.current.status;
          console.log(`[vox] event: continuous-toggle (state=${s})`);
          // Press toggles continuous mode. If already armed (continuousActiveRef
          // true), turn it off and cancel any active session. If off, arm it
          // and start a session if idle. Auto-stop must also be enabled —
          // sidecar drives segment boundaries on silence.
          if (continuousActiveRef.current) {
            console.log("[vox] continuous: disarming");
            continuousActiveRef.current = false;
            if (s === "recording" || s === "streaming") {
              cancelStream();
              dispatch({ type: "CANCEL_RECORDING" });
            }
            return;
          }
          // Force-enable auto-stop in the persisted settings so the sidecar
          // segments on silence — otherwise continuous mode has no way to end
          // a segment without a user gesture.
          try {
            const raw = localStorage.getItem("vox_settings");
            const parsed = raw ? JSON.parse(raw) : {};
            parsed.autoStopEnabled = true;
            parsed.continuousMode = true;
            localStorage.setItem("vox_settings", JSON.stringify(parsed));
          } catch {
            // best effort
          }
          fetch(`http://127.0.0.1:${sidecarPort}/settings/dictation`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ auto_stop_enabled: true }),
          }).catch(() => {});

          continuousActiveRef.current = true;
          if (s === "idle") {
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
          }
        }),
      );

      cleanups.push(
        await listen<null>("undo-pressed", () => {
          const s = stateRef.current.status;
          console.log(`[vox] event: undo-pressed (state=${s})`);
          // Rust already sent Ctrl+Z to the target. Re-record only from idle to
          // avoid stomping on an in-flight session.
          if (s !== "idle") {
            return;
          }
          invoke<DeepContextPayload>("get_focused_context")
            .then((ctx) => {
              contextRef.current = ctx;
            })
            .catch(() => {
              contextRef.current = null;
            });
          invoke("position_overlay").catch(() => {});
          levelRef.current = 0;
          lastLevelTimeRef.current = 0;
          speechDetectedRef.current = false;
          recordingStartTimeRef.current = Date.now();
          beginStream();
          dispatch({ type: "START_RECORDING" });
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

  // Esc-to-cancel during transcript preview. window-level capture so the
  // overlay's mouse-transparent / no-focus posture doesn't swallow the key.
  useEffect(() => {
    if (state.status !== "previewing") return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        dispatch({ type: "PREVIEW_CANCEL" });
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [state.status]);

  // Recording-state safety timeout: if no audio_level event arrives in 10 s,
  // mic or WS pipeline is dead — bail out.
  useEffect(() => {
    if (state.status !== "recording") return;
    const id = setTimeout(() => {
      if (lastLevelTimeRef.current === 0) {
        dispatch({ type: "ERROR", message: "Mic not responding" });
      }
    }, 10_000);
    return () => clearTimeout(id);
  }, [state.status]);

  useEffect(() => {
    console.log("[vox] state ->", state.status, state);
  }, [state]);

  // Park window off-screen when idle — Rust moves it on-screen on hotkey press.
  // setPosition keeps WebView2 compositor active (no reinit, no black/transparent flash).
  useEffect(() => {
    if (state.status === "idle") {
      // Continuous mode (Tier 2 #10): if a session just finished and the
      // continuous flag is still armed, restart the recording instead of
      // parking. Window stays in place; capsule re-renders bars.
      if (continuousActiveRef.current) {
        console.log("[vox] continuous: restarting recording after inject");
        invoke("position_overlay").catch(() => {});
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
        return;
      }
      console.log("[vox] window: parking at -10000,-10000 (state=idle)");
      getCurrentWindow()
        .setPosition(new LogicalPosition(-10000, -10000))
        .then(() => console.log("[vox] window: parked ok"))
        .catch((e: unknown) => console.error("[vox] window: park failed", e));
    }
  }, [state.status, beginStream]);

  return (
    <StreamingOverlay
      status={state.status}
      partial={
        state.status === "streaming"
          ? state.partial
          : state.status === "previewing"
            ? state.transcript
            : ""
      }
      degraded={state.status === "degraded" ? state.missing : []}
      errorMessage={state.status === "error" ? state.message : ""}
      processingMsg={state.status === "finalizing" ? processingMsg : ""}
      levelRef={levelRef}
      lastLevelTimeRef={lastLevelTimeRef}
      bandsRef={bandsRef}
      onCancel={() => {
        cancelStream();
        dispatch({ type: "CANCEL_RECORDING" });
      }}
    />
  );
}
