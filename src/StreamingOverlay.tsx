import React from "react";

// ── Types ─────────────────────────────────────────────────────────────────────

export type AppStatus =
  | "waiting_for_models"
  | "degraded"
  | "idle"
  | "recording"
  | "streaming"
  | "finalizing"
  | "previewing"
  | "injecting"
  | "error";

export interface StreamingOverlayProps {
  status: AppStatus;
  partial: string;
  degraded: string[];
  errorMessage: string;
  processingMsg: string;
  levelRef: React.MutableRefObject<number>;
  lastLevelTimeRef: React.MutableRefObject<number>;
  bandsRef: React.MutableRefObject<number[]>;
  onCancel: () => void;
}

// ── Constants ────────────────────────────────────────────────────────────────

// 11 bars — matches `audio_level.bands` from the sidecar 1:1. See
// `audio/capture.PersistentMicCapture._compute_band_amplitudes`.
const NUM_BARS = 11;

// Variant #10 — squared compact pill. 14 px corner radius, larger bars.
const MIN_BAR_H = 3;
const MAX_BAR_H = 17;

const BAR_SPEEDS = [89, 68, 51, 79, 58, 97, 47, 72, 106, 62, 76];

// Bar color per status. Frozen states ease to a uniform low baseline while
// the inline spinner (right of bars) conveys active processing.
const STATE_COLORS: Record<AppStatus, string> = {
  waiting_for_models: "rgba(255,255,255,0.45)",
  degraded: "#ffd28a",
  idle: "rgba(255,255,255,0.6)",
  recording: "#ffffff",
  streaming: "#ffffff",
  finalizing: "rgba(255,255,255,0.55)", // dimmed white — paused, spinner does the work
  previewing: "#cdb8ff",
  injecting: "#b8ffb8",
  error: "#ff8a8a",
};

// Frozen-bar baseline per non-animated state. Uniform low height across all
// bars so the pill reads as "at rest, processing" — not "stuck mid-wave".
// Live bars ease into this baseline over ~250 ms (see useWaveformAnimation),
// so the transition from streaming → finalizing settles instead of snapping.
const FROZEN_BASELINE: Partial<Record<AppStatus, number>> = {
  finalizing: 0.14,
  injecting: 0.2,
  previewing: 0.18,
  error: 0.12,
  degraded: 0.1,
};

// ── CSS (single style tag) ───────────────────────────────────────────────────

const OVERLAY_STYLES = `
  @keyframes vox-spin { to { transform: rotate(360deg); } }

  /* Variant #10 — squared compact pill */
  .vox-pill {
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 2px;
    padding: 6px 14px;
    background: linear-gradient(180deg, #1c1d22 0%, #08090b 100%);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 14px;
    box-shadow:
      inset 0 1px 0 rgba(255,255,255,0.05),
      0 4px 10px rgba(0,0,0,0.35);
    transition: box-shadow 200ms ease-out, opacity 200ms ease-out;
  }
  .vox-pill[data-state="error"] {
    box-shadow:
      inset 0 1px 0 rgba(255,255,255,0.05),
      0 0 12px 1px rgba(255, 100, 100, 0.55),
      0 4px 10px rgba(0,0,0,0.35);
  }
  .vox-pill[data-state="recording"],
  .vox-pill[data-state="streaming"] {
    box-shadow:
      inset 0 1px 0 rgba(255,255,255,0.05),
      0 0 8px 0.5px rgba(255,255,255,0.10),
      0 4px 10px rgba(0,0,0,0.35);
  }
  .vox-pill[data-state="injecting"] { opacity: 0.92; }

  .vox-bar {
    width: 2.5px;
    height: ${MIN_BAR_H}px;
    border-radius: 2px;
    flex-shrink: 0;
    transition: background-color 180ms ease-out;
  }

  /* Inline loader — focal point of the processing capsule. Sits inside the
     pill, right of the bars, during finalizing/injecting/previewing.
     Full opacity from frame 1 — no entry animation, no fade. The loader IS
     the "I saw your key release" signal; any opacity ramp delays that. */
  .vox-loader {
    width: 18px;
    height: 18px;
    margin-left: 10px;
    flex-shrink: 0;
    border-radius: 50%;
    border: 2.2px solid rgba(255,255,255,0.25);
    border-top-color: #ffffff;
    animation: vox-spin 0.5s linear infinite;
    box-sizing: border-box;
    box-shadow: 0 0 6px rgba(255,255,255,0.18);
  }

  /* Bars dim instantly in processing states so the loader dominates. */
  .vox-pill[data-state="finalizing"] .vox-bar,
  .vox-pill[data-state="injecting"] .vox-bar,
  .vox-pill[data-state="previewing"] .vox-bar {
    opacity: 0.35;
  }
`;

// ── Bar animation hook ───────────────────────────────────────────────────────

function useWaveformAnimation(
  barsRef: React.MutableRefObject<Array<HTMLDivElement | null>>,
  levelRef: React.MutableRefObject<number>,
  lastLevelTimeRef: React.MutableRefObject<number>,
  bandsRef: React.MutableRefObject<number[]>,
  status: AppStatus,
) {
  const smoothedRef = React.useRef<number[]>(new Array(NUM_BARS).fill(0));
  const rafRef = React.useRef<number>();

  React.useEffect(() => {
    const animate = () => {
      const t = Date.now();
      const hasRealLevel = t - lastLevelTimeRef.current < 400;
      const bands = bandsRef.current;
      const hasRealBands =
        hasRealLevel && Array.isArray(bands) && bands.length === NUM_BARS;

      const baseline = FROZEN_BASELINE[status];

      barsRef.current.forEach((bar, i) => {
        if (!bar) return;

        let target: number;
        if (baseline !== undefined) {
          // Frozen state: ease toward uniform low baseline. Same smoothing
          // pipeline as live bars below — peak → baseline settles over
          // ~250 ms instead of snapping. Reads as "calm + processing".
          target = baseline;
          // Hard snap to baseline on first frozen frame — no smoothing.
          // The loader (rendered the same frame state transitions) is the
          // sole "processing" signal; bars must commit to a static rest
          // shape immediately so they don't compete for attention.
          const next = target;
          smoothedRef.current[i] = next;
          const h = MIN_BAR_H + next * (MAX_BAR_H - MIN_BAR_H);
          bar.style.height = `${Math.max(MIN_BAR_H, h)}px`;
          return;
        } else {
          // Always-on flowing baseline: two-harmonic sine per bar with unique
          // phase + speed so the pill never looks flat, even during silence.
          // Range ~0.05 → 0.55, organic wave-like motion across all 11 bars.
          const spd = BAR_SPEEDS[i];
          const ph = (i / NUM_BARS) * Math.PI * 2;
          const baseFlow =
            0.28 +
            0.18 * Math.sin(t / spd + ph) +
            0.1 * Math.sin(t / (spd * 1.7) + ph * 1.3);

          if (hasRealBands) {
            // Live audio: expand the band, blend with the baseline so loud
            // peaks dominate but quiet input still rides the wave instead
            // of collapsing to a dead-flat row of dots.
            const raw = Math.min(1, Math.max(0, bands[i]));
            const expanded = Math.pow(raw, 0.5);
            target = Math.max(baseFlow, expanded);
          } else if (hasRealLevel) {
            // Scalar level only (older sidecar build, no per-band data).
            const raw = Math.min(1, levelRef.current * 12);
            target = Math.max(baseFlow, raw * 0.9);
          } else {
            // No audio events at all — pure baseline motion.
            target = baseFlow;
          }
        }

        const prev = smoothedRef.current[i];
        const next =
          target > prev
            ? prev * 0.15 + target * 0.85
            : prev * 0.72 + target * 0.28;
        smoothedRef.current[i] = next;

        const h = MIN_BAR_H + next * (MAX_BAR_H - MIN_BAR_H);
        bar.style.height = `${Math.max(MIN_BAR_H, h)}px`;
      });

      rafRef.current = requestAnimationFrame(animate);
    };
    rafRef.current = requestAnimationFrame(animate);
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
    };
  }, [barsRef, levelRef, lastLevelTimeRef, bandsRef, status]);
}

// ── Main export ──────────────────────────────────────────────────────────────

export default function StreamingOverlay({
  status,
  levelRef,
  lastLevelTimeRef,
  bandsRef,
}: StreamingOverlayProps) {
  const barsRef = React.useRef<Array<HTMLDivElement | null>>(
    new Array(NUM_BARS).fill(null),
  );

  useWaveformAnimation(barsRef, levelRef, lastLevelTimeRef, bandsRef, status);

  if (status === "idle" || status === "waiting_for_models") {
    // Empty render — Rust parks the window off-screen for these states.
    return <style>{OVERLAY_STYLES}</style>;
  }

  const color = STATE_COLORS[status];
  // Spinner visible while sidecar/LLM/inject pipeline runs.
  const showLoader =
    status === "finalizing" ||
    status === "injecting" ||
    status === "previewing";

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
        <div className="vox-pill" data-state={status}>
          {Array.from({ length: NUM_BARS }, (_, i) => (
            <div
              key={i}
              ref={(el) => {
                barsRef.current[i] = el;
              }}
              className="vox-bar"
              style={{ background: color }}
            />
          ))}
          {showLoader && <div className="vox-loader" />}
        </div>
      </div>
    </>
  );
}
