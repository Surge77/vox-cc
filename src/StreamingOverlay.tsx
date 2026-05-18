import React from "react";

// ── Types ─────────────────────────────────────────────────────────────────────

export type AppStatus =
  | "waiting_for_models"
  | "degraded"
  | "idle"
  | "recording"
  | "streaming"
  | "finalizing"
  | "injecting"
  | "error";

export interface StreamingOverlayProps {
  status: AppStatus;
  partial: string;
  degraded: string[];
  levelRef: React.MutableRefObject<number>;
  lastLevelTimeRef: React.MutableRefObject<number>;
}

// ── Waveform constants ────────────────────────────────────────────────────────

// Pearl Compact (v2) — 11-bar EQ profile
const BAR_MULTIPLIERS = [
  0.22, 0.46, 0.72, 0.96, 0.82, 0.58, 0.78, 0.4, 0.25, 0.52, 0.88,
];
// Each bar oscillates at its own period (ms). Prime-ish values prevent synchronisation.
const BAR_SPEEDS = [127, 97, 73, 113, 83, 139, 67, 103, 151, 89, 109];
const NUM_BARS = BAR_MULTIPLIERS.length;
const MIN_BAR_H = 4;
const MAX_BAR_H = 28;

// ── CSS ───────────────────────────────────────────────────────────────────────

export const OVERLAY_STYLES = `
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

// ── Style objects ─────────────────────────────────────────────────────────────

export const CAPSULE: React.CSSProperties = {
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

export const PEARL_PILL: React.CSSProperties = {
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

// ── Sub-components ────────────────────────────────────────────────────────────

export function WaveformBars({
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

export function Spinner() {
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

// ── Bar animation hook ────────────────────────────────────────────────────────

export function useWaveformAnimation(
  barsRef: React.MutableRefObject<Array<HTMLDivElement | null>>,
  levelRef: React.MutableRefObject<number>,
  lastLevelTimeRef: React.MutableRefObject<number>,
) {
  const smoothedRef = React.useRef(0);
  const rafRef = React.useRef<number>();

  React.useEffect(() => {
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
  }, [barsRef, levelRef, lastLevelTimeRef]);
}

// ── Overlay card ──────────────────────────────────────────────────────────────

function OverlayCard({
  status,
  degraded,
  barsRef,
}: {
  status: AppStatus;
  degraded: string[];
  barsRef: React.MutableRefObject<Array<HTMLDivElement | null>>;
}) {
  if (status === "idle") return null;

  if (status === "waiting_for_models") {
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

  if (status === "degraded") {
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
        <span style={{ color: "#b91c1c" }}>
          Degraded{degraded.length > 0 ? `: ${degraded.join(", ")}` : ""}
        </span>
      </div>
    );
  }

  if (status === "error") {
    return (
      <div style={{ ...PEARL_PILL, borderColor: "rgba(239, 68, 68, 0.25)" }}>
        <span style={{ color: "#b91c1c" }}>Error</span>
      </div>
    );
  }

  if (status === "recording" || status === "streaming") {
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

  if (status === "finalizing") {
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

  if (status === "injecting") {
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

// ── Main export ───────────────────────────────────────────────────────────────

export default function StreamingOverlay({
  status,
  degraded,
  levelRef,
  lastLevelTimeRef,
}: StreamingOverlayProps) {
  const barsRef = React.useRef<Array<HTMLDivElement | null>>(
    new Array(NUM_BARS).fill(null),
  );

  useWaveformAnimation(barsRef, levelRef, lastLevelTimeRef);

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
        <OverlayCard status={status} degraded={degraded} barsRef={barsRef} />
      </div>
    </>
  );
}
