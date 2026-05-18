import { useState, useEffect, useRef } from "react";

// ── Port discovery ────────────────────────────────────────────────────────────

const DEFAULT_PORT = 8000;
let sidecarPort = DEFAULT_PORT;

async function discoverSidecarPort(): Promise<number> {
  for (let port = 8000; port <= 8009; port++) {
    try {
      const resp = await fetch(`http://127.0.0.1:${port}/health`, {
        signal: AbortSignal.timeout(500),
      });
      if (resp.ok) {
        sidecarPort = port;
        return port;
      }
    } catch {
      continue;
    }
  }
  return DEFAULT_PORT;
}

const API = (path: string) => `http://127.0.0.1:${sidecarPort}${path}`;

// ── Settings (read-only — collection toggle lives in Settings window) ─────────

interface VoxSettings {
  passiveCollectionEnabled: boolean;
}

const SETTINGS_KEY = "vox_settings";

function loadPassiveCollectionEnabled(): boolean {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (!raw) return false;
    const parsed = JSON.parse(raw) as Partial<VoxSettings>;
    return parsed.passiveCollectionEnabled ?? false;
  } catch {
    return false;
  }
}

// ── Types ─────────────────────────────────────────────────────────────────────

interface FinetuneStatus {
  status: "running" | "idle" | "complete" | "error";
  progress: number;
  epoch: number;
  total_epochs: number;
  samples: number;
  error: string | null;
}

const DEFAULT_STATUS: FinetuneStatus = {
  status: "idle",
  progress: 0,
  epoch: 0,
  total_epochs: 3,
  samples: 0,
  error: null,
};

// ── Styles ────────────────────────────────────────────────────────────────────

const ROOT: React.CSSProperties = {
  fontFamily: "system-ui, -apple-system, sans-serif",
  fontSize: 16,
  color: "#1a1a1a",
  background: "#fff",
  minHeight: "100vh",
  padding: 0,
  margin: 0,
};

const CONTAINER: React.CSSProperties = {
  maxWidth: 480,
  margin: "0 auto",
  padding: "24px 20px 40px",
};

const PAGE_TITLE: React.CSSProperties = {
  fontSize: 20,
  fontWeight: 600,
  margin: "0 0 24px",
  color: "#1a1a1a",
};

const SECTION: React.CSSProperties = {
  paddingBottom: 20,
  marginBottom: 20,
  borderBottom: "1px solid #eee",
};

const SECTION_LAST: React.CSSProperties = {
  paddingBottom: 0,
  marginBottom: 0,
};

const SECTION_LABEL: React.CSSProperties = {
  fontSize: 13,
  fontWeight: 600,
  color: "#666",
  textTransform: "uppercase" as const,
  letterSpacing: "0.05em",
  marginBottom: 12,
};

const HINT: React.CSSProperties = {
  fontSize: 13,
  color: "#888",
  marginTop: 4,
};

const STAT_ROW: React.CSSProperties = {
  display: "flex",
  alignItems: "baseline",
  gap: 8,
  marginBottom: 6,
};

const STAT_VALUE: React.CSSProperties = {
  fontSize: 28,
  fontWeight: 700,
  color: "#1a1a1a",
  lineHeight: 1,
};

const STAT_UNIT: React.CSSProperties = {
  fontSize: 14,
  color: "#888",
};

const BTN: React.CSSProperties = {
  padding: "9px 18px",
  border: "none",
  borderRadius: 6,
  fontSize: 14,
  fontWeight: 500,
  cursor: "pointer",
  background: "#1a1a1a",
  color: "#fff",
};

const BTN_DISABLED: React.CSSProperties = {
  ...BTN,
  background: "#ccc",
  cursor: "not-allowed",
};

const PROGRESS_TRACK: React.CSSProperties = {
  width: "100%",
  height: 8,
  background: "#eee",
  borderRadius: 999,
  overflow: "hidden",
  marginTop: 12,
};

const BADGE_OK: React.CSSProperties = {
  display: "inline-block",
  marginTop: 12,
  fontSize: 14,
  color: "#16a34a",
  fontWeight: 500,
};

const BADGE_ERR: React.CSSProperties = {
  display: "inline-block",
  marginTop: 12,
  fontSize: 13,
  color: "#dc2626",
};

const STATUS_PILL: React.CSSProperties = {
  display: "inline-block",
  padding: "2px 10px",
  borderRadius: 999,
  fontSize: 12,
  fontWeight: 600,
  letterSpacing: "0.04em",
};

// ── Component ─────────────────────────────────────────────────────────────────

export default function FineTuningDashboard() {
  const [finetuneStatus, setFinetuneStatus] =
    useState<FinetuneStatus>(DEFAULT_STATUS);
  const [portReady, setPortReady] = useState(false);
  const [starting, setStarting] = useState(false);
  const [collectionEnabled] = useState(loadPassiveCollectionEnabled);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout>>();

  async function fetchStatus(): Promise<FinetuneStatus> {
    const resp = await fetch(API("/finetune/status"), {
      signal: AbortSignal.timeout(3000),
    });
    return (await resp.json()) as FinetuneStatus;
  }

  function scheduleNextPoll(latestStatus: FinetuneStatus) {
    clearTimeout(pollTimerRef.current);
    if (latestStatus.status === "running") {
      pollTimerRef.current = setTimeout(async () => {
        try {
          const next = await fetchStatus();
          setFinetuneStatus(next);
          scheduleNextPoll(next);
        } catch {
          scheduleNextPoll(latestStatus);
        }
      }, 2000);
    }
  }

  useEffect(() => {
    discoverSidecarPort().then(async () => {
      setPortReady(true);
      try {
        const status = await fetchStatus();
        setFinetuneStatus(status);
        scheduleNextPoll(status);
      } catch {
        // sidecar not yet ready — leave default status
      }
    });

    return () => {
      clearTimeout(pollTimerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function startTraining() {
    setStarting(true);
    try {
      await fetch(API("/finetune/start"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ epochs: 3, learning_rate: 3e-4 }),
      });
      // immediately poll so UI reflects "running"
      const status = await fetchStatus();
      setFinetuneStatus(status);
      scheduleNextPoll(status);
    } catch {
      setFinetuneStatus((prev) => ({
        ...prev,
        status: "error",
        error: "Failed to start training — is the sidecar running?",
      }));
    } finally {
      setStarting(false);
    }
  }

  const { status, progress, epoch, total_epochs, samples, error } =
    finetuneStatus;

  const isRunning = status === "running";
  const isComplete = status === "complete";
  const isError = status === "error";

  const canStart =
    samples >= 50 && !isRunning && !starting && status !== "complete";

  function statusColor(): string {
    if (isRunning) return "#d97706";
    if (isComplete) return "#16a34a";
    if (isError) return "#dc2626";
    return "#888";
  }

  function statusLabel(): string {
    if (isRunning) return "Running";
    if (isComplete) return "Complete";
    if (isError) return "Error";
    return "Idle";
  }

  return (
    <div style={ROOT}>
      <div style={CONTAINER}>
        <h1 style={PAGE_TITLE}>Fine-Tuning</h1>

        {/* Sample count */}
        <div style={SECTION}>
          <div style={SECTION_LABEL}>Training Data</div>
          <div style={STAT_ROW}>
            <span style={STAT_VALUE}>{samples}</span>
            <span style={STAT_UNIT}>samples collected</span>
          </div>

          {samples === 0 && !collectionEnabled && (
            <p style={{ ...HINT, color: "#d97706" }}>
              Passive collection must be enabled in Settings to collect training
              data.
            </p>
          )}
          {samples === 0 && collectionEnabled && (
            <p style={HINT}>
              Collection is active. Samples appear here after each dictation
              session.
            </p>
          )}
          {samples > 0 && samples < 50 && (
            <p style={HINT}>
              {50 - samples} more samples needed before training can start.
            </p>
          )}
          {samples >= 50 && (
            <p style={{ ...HINT, color: "#16a34a" }}>
              Enough samples to start training.
            </p>
          )}
        </div>

        {/* Training status + progress */}
        {!portReady ? (
          <div style={SECTION}>
            <div style={SECTION_LABEL}>Status</div>
            <p style={HINT}>Connecting to sidecar…</p>
          </div>
        ) : (
          <div style={SECTION}>
            <div style={SECTION_LABEL}>Status</div>

            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <span
                style={{
                  ...STATUS_PILL,
                  background:
                    statusColor() === "#888" ? "#f0f0f0" : `${statusColor()}18`,
                  color: statusColor(),
                }}
              >
                {statusLabel()}
              </span>
              {isRunning && (
                <span style={{ fontSize: 13, color: "#888" }}>
                  Epoch {epoch} / {total_epochs}
                </span>
              )}
            </div>

            {isRunning && (
              <>
                <div style={PROGRESS_TRACK}>
                  <div
                    style={{
                      height: "100%",
                      width: `${Math.round(progress * 100)}%`,
                      background: "#1a1a1a",
                      borderRadius: 999,
                      transition: "width 0.4s ease",
                    }}
                  />
                </div>
                <p style={HINT}>{Math.round(progress * 100)}% complete</p>
              </>
            )}

            {isComplete && (
              <span style={BADGE_OK}>Training complete — model updated ✓</span>
            )}

            {isError && error && <span style={BADGE_ERR}>{error}</span>}
          </div>
        )}

        {/* Start training */}
        <div style={SECTION_LAST}>
          <div style={SECTION_LABEL}>Train Model</div>
          <button
            style={canStart ? BTN : BTN_DISABLED}
            disabled={!canStart}
            onClick={startTraining}
          >
            {starting
              ? "Starting…"
              : isRunning
                ? "Training in progress…"
                : "Start Training"}
          </button>

          {!canStart && !isRunning && !isComplete && !starting && (
            <p style={HINT}>
              {samples < 50
                ? `Need ${50 - samples} more samples (${samples}/50).`
                : ""}
            </p>
          )}
          <p style={HINT}>
            Runs 3 epochs of LoRA fine-tuning on distil-large-v3 using your
            collected samples. Takes 10–30 minutes on GTX 1650. Dictation is
            blocked during training.
          </p>
        </div>
      </div>
    </div>
  );
}
