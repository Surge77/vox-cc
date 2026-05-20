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
  errorMessage: string;
  processingMsg: string;
  levelRef: React.MutableRefObject<number>;
  lastLevelTimeRef: React.MutableRefObject<number>;
  bandsRef: React.MutableRefObject<number[]>;
  onCancel: () => void;
}

// ── Waveform constants ────────────────────────────────────────────────────────

// Pearl Compact (v2) — 11-bar EQ profile
const BAR_MULTIPLIERS = [
  0.22, 0.46, 0.72, 0.96, 0.82, 0.58, 0.78, 0.4, 0.25, 0.52, 0.88,
];
// Each bar oscillates at its own period (ms). Reduced ~30% from original for more reactive animation.
const BAR_SPEEDS = [89, 68, 51, 79, 58, 97, 47, 72, 106, 62, 76];
const NUM_BARS = BAR_MULTIPLIERS.length;
const MIN_BAR_H = 3;
const MAX_BAR_H = 34; // was 28 — wider dynamic range for more visible motion

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
  bandsRef: React.MutableRefObject<number[]>,
) {
  // Per-bar smoothed values — each bar tracks its own band with fast attack /
  // slow decay so the spectrum reads as a real-time analyzer not a sine wave.
  const smoothedBandsRef = React.useRef<number[]>(new Array(NUM_BARS).fill(0));
  const rafRef = React.useRef<number>();

  React.useEffect(() => {
    const animate = () => {
      const t = Date.now();
      const hasRealLevel = t - lastLevelTimeRef.current < 400;
      const bands = bandsRef.current;
      const hasRealBands =
        hasRealLevel && Array.isArray(bands) && bands.length === NUM_BARS;

      // Idle floor — gentle motion when no level events / no bands.
      const idleLevel = 0.18;

      barsRef.current.forEach((bar, i) => {
        if (!bar) return;

        // Real-spectrum branch: bar i ← FFT band i (with per-bar attack/decay).
        // Fallback branch: two-frequency compound sine like before, modulated
        // by the legacy single-scalar level (back-compat with old sidecars).
        let target: number;
        if (hasRealBands) {
          // Aggressive gamma (0.5) expands mid range so quiet SST audio
          // moves bars dramatically. Add a small per-bar wobble that's
          // independent of the spectrum so even a held vowel doesn't look
          // frozen — bars "breathe" instead of locking at one height.
          const raw = Math.min(1, Math.max(0, bands[i]));
          const expanded = Math.pow(raw, 0.5);
          const spd = BAR_SPEEDS[i];
          const ph = (i / NUM_BARS) * Math.PI * 2;
          const wobble = 0.08 * Math.sin(t / spd + ph); // ±8% organic jitter
          target = Math.max(0, Math.min(1, expanded + wobble));
        } else if (hasRealLevel) {
          const raw = Math.min(1, levelRef.current * 12);
          const spd = BAR_SPEEDS[i];
          const ph1 = (i / NUM_BARS) * Math.PI * 2;
          const ph2 = ph1 + 2.1;
          const osc =
            Math.sin(t / spd + ph1) * 0.65 +
            Math.sin(t / (spd * 1.6) + ph2) * 0.35;
          target = (idleLevel + raw * 0.82) * ((osc + 1) / 2);
        } else {
          // Pure idle motion — slow sine, very low amplitude.
          const ph = (i / NUM_BARS) * Math.PI * 2;
          target = idleLevel * (0.6 + 0.4 * Math.sin(t / 480 + ph));
        }

        // Per-bar smoothing — looser than before so bars actually swing.
        // Attack: 0.85 (was 0.8) — snappier rise on speech onset.
        // Decay:  0.72 (was 0.88) — bars fall faster, won't appear "stuck".
        const prev = smoothedBandsRef.current[i];
        const next =
          target > prev
            ? prev * 0.15 + target * 0.85
            : prev * 0.72 + target * 0.28;
        smoothedBandsRef.current[i] = next;

        const peak = MIN_BAR_H + BAR_MULTIPLIERS[i] * (MAX_BAR_H - MIN_BAR_H);
        const h = MIN_BAR_H + next * (peak - MIN_BAR_H);
        bar.style.height = `${Math.max(MIN_BAR_H, h)}px`;
      });

      rafRef.current = requestAnimationFrame(animate);
    };

    rafRef.current = requestAnimationFrame(animate);
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
    };
  }, [barsRef, levelRef, lastLevelTimeRef, bandsRef]);
}

// ── Overlay card ──────────────────────────────────────────────────────────────

function OverlayCard({
  status,
  partial,
  degraded,
  errorMessage,
  processingMsg,
  barsRef,
  onCancel,
}: {
  status: AppStatus;
  partial: string;
  degraded: string[];
  errorMessage: string;
  processingMsg: string;
  barsRef: React.MutableRefObject<Array<HTMLDivElement | null>>;
  onCancel: () => void;
}) {
  if (status === "idle" || status === "waiting_for_models") return null;

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
        <span style={{ color: "#b91c1c" }}>{errorMessage || "Error"}</span>
      </div>
    );
  }

  if (status === "recording" || status === "streaming") {
    const hasPartial = !!partial;
    const trailingLabel = hasPartial ? "" : "Listening…";

    return (
      <div className="vox-capsule vox-capsule-active" style={CAPSULE}>
        <button
          className="vox-icon-x"
          onClick={onCancel}
          aria-label="Cancel recording"
          style={{ cursor: "pointer" }}
        />
        {/* Waveform always visible during capture — continuous "still recording"
            signal. Sits left of the partial transcript so the bars keep moving
            even as text grows. */}
        <WaveformBars barsRef={barsRef} />
        {hasPartial && (
          <span
            style={{
              flex: "1 1 auto",
              minWidth: 0,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              direction: "rtl", // newest words stay visible on the right
              textAlign: "left",
            }}
            title={partial}
          >
            {/* bidi-LTR wrapper so rtl direction only affects clipping,
                not character order */}
            <bdo dir="ltr">{partial}</bdo>
          </span>
        )}
        {trailingLabel && (
          <span
            style={{
              color: "#5a6470",
              fontSize: 12,
              fontWeight: 500,
              flexShrink: 0,
              letterSpacing: "0.01em",
            }}
          >
            {trailingLabel}
          </span>
        )}
      </div>
    );
  }

  if (status === "finalizing" || status === "injecting") {
    return (
      <div className="vox-capsule" style={CAPSULE}>
        <Spinner />
        <span
          style={{
            color: "#5a6470",
            fontSize: 12,
            fontWeight: 500,
            letterSpacing: "0.01em",
          }}
        >
          {status === "finalizing"
            ? processingMsg || "Processing…"
            : "Injecting…"}
        </span>
      </div>
    );
  }

  return null;
}

// ── Main export ───────────────────────────────────────────────────────────────

export default function StreamingOverlay({
  status,
  partial,
  degraded,
  errorMessage,
  processingMsg,
  levelRef,
  lastLevelTimeRef,
  bandsRef,
  onCancel,
}: StreamingOverlayProps) {
  const barsRef = React.useRef<Array<HTMLDivElement | null>>(
    new Array(NUM_BARS).fill(null),
  );

  useWaveformAnimation(barsRef, levelRef, lastLevelTimeRef, bandsRef);

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
        <OverlayCard
          status={status}
          partial={partial}
          degraded={degraded}
          errorMessage={errorMessage}
          processingMsg={processingMsg}
          barsRef={barsRef}
          onCancel={onCancel}
        />
      </div>
    </>
  );
}
