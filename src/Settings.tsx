import { useState, useEffect } from "react";

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

// ── Settings persistence ──────────────────────────────────────────────────────

interface VoxSettings {
  useLlm: boolean;
  useGroq: boolean;
  hasGroqKey: boolean;
  audioDeviceIndex: number;
  vocabulary: string[];
  passiveCollectionEnabled: boolean;
  defaultStyle: string;
  retentionDays: 0 | 7 | 30 | null;
}

const DEFAULT_SETTINGS: VoxSettings = {
  useLlm: true,
  useGroq: false,
  hasGroqKey: false,
  audioDeviceIndex: -1,
  vocabulary: [],
  passiveCollectionEnabled: false,
  defaultStyle: "auto",
  retentionDays: null,
};

const SETTINGS_KEY = "vox_settings";

function loadSettings(): VoxSettings {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    return raw ? { ...DEFAULT_SETTINGS, ...JSON.parse(raw) } : DEFAULT_SETTINGS;
  } catch {
    return DEFAULT_SETTINGS;
  }
}

function saveSettings(s: VoxSettings) {
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(s));
}

// ── Types ─────────────────────────────────────────────────────────────────────

interface AudioDevice {
  index: number;
  name: string;
  default: boolean;
  bluetooth_warning?: boolean;
}

type GroqStatus = "idle" | "testing" | "ok" | "error";

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

const ROW: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 10,
  marginBottom: 10,
};

const INPUT: React.CSSProperties = {
  flex: 1,
  padding: "7px 10px",
  border: "1px solid #ddd",
  borderRadius: 6,
  fontSize: 14,
  color: "#1a1a1a",
  background: "#fafafa",
  outline: "none",
};

const SELECT: React.CSSProperties = {
  ...INPUT,
  cursor: "pointer",
};

const TEXTAREA: React.CSSProperties = {
  width: "100%",
  minHeight: 100,
  padding: "8px 10px",
  border: "1px solid #ddd",
  borderRadius: 6,
  fontSize: 14,
  color: "#1a1a1a",
  background: "#fafafa",
  resize: "vertical" as const,
  fontFamily: "inherit",
  boxSizing: "border-box" as const,
};

const BTN: React.CSSProperties = {
  padding: "7px 14px",
  border: "none",
  borderRadius: 6,
  fontSize: 14,
  fontWeight: 500,
  cursor: "pointer",
  background: "#1a1a1a",
  color: "#fff",
  flexShrink: 0,
};

const BTN_DISABLED: React.CSSProperties = {
  ...BTN,
  background: "#ccc",
  cursor: "not-allowed",
};

const BADGE_OK: React.CSSProperties = {
  fontSize: 13,
  color: "#16a34a",
  fontWeight: 500,
};

const BADGE_ERR: React.CSSProperties = {
  fontSize: 13,
  color: "#dc2626",
};

const HINT: React.CSSProperties = {
  fontSize: 13,
  color: "#888",
  marginTop: 4,
};

const TOGGLE_LABEL: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 10,
  cursor: "pointer",
  userSelect: "none" as const,
};

const BADGE_SAVED: React.CSSProperties = {
  fontSize: 13,
  color: "#16a34a",
  fontWeight: 500,
  marginTop: 8,
};

// ── Component ─────────────────────────────────────────────────────────────────

export default function Settings() {
  const [settings, setSettings] = useState<VoxSettings>(loadSettings);
  const [devices, setDevices] = useState<AudioDevice[]>([]);
  const [groqKeyInput, setGroqKeyInput] = useState("");
  const [groqStatus, setGroqStatus] = useState<GroqStatus>("idle");
  const [groqError, setGroqError] = useState("");
  const [vocabText, setVocabText] = useState(() =>
    loadSettings().vocabulary.join("\n"),
  );
  const [vocabSaved, setVocabSaved] = useState(false);
  const [portReady, setPortReady] = useState(false);
  const [snippets, setSnippets] = useState<Record<string, string>>({});
  const [snippetTrigger, setSnippetTrigger] = useState("");
  const [snippetExpansion, setSnippetExpansion] = useState("");

  useEffect(() => {
    discoverSidecarPort().then(() => {
      setPortReady(true);
      fetch(API("/audio/devices"))
        .then((r) => r.json())
        .then((data: { devices?: AudioDevice[] }) =>
          setDevices(data.devices ?? []),
        )
        .catch(() => {});
      fetch(API("/snippets"))
        .then((r) => r.json())
        .then((data: Record<string, string>) => setSnippets(data))
        .catch(() => {});
    });
  }, []);

  function update(patch: Partial<VoxSettings>) {
    const next = { ...settings, ...patch };
    setSettings(next);
    saveSettings(next);
  }

  async function saveGroqKey() {
    if (!groqKeyInput.trim()) return;
    setGroqStatus("testing");
    setGroqError("");
    try {
      const resp = await fetch(API("/finetune/groq-test"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          api_key: groqKeyInput.trim(),
          raw_transcript: "test",
        }),
      });
      const data = (await resp.json()) as { ok: boolean; error?: string };
      if (data.ok) {
        update({ hasGroqKey: true, useGroq: true });
        setGroqStatus("ok");
        setGroqKeyInput("");
      } else {
        setGroqStatus("error");
        setGroqError(data.error ?? "Invalid key");
      }
    } catch {
      setGroqStatus("error");
      setGroqError("Request failed — is the sidecar running?");
    }
  }

  async function togglePassiveCollection(enabled: boolean) {
    update({ passiveCollectionEnabled: enabled });
    try {
      await fetch(API("/finetune/toggle-collection"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
    } catch {
      // non-fatal — setting already persisted locally
    }
  }

  async function saveVocabulary() {
    const words = vocabText
      .split("\n")
      .map((w) => w.trim())
      .filter((w) => w.length > 0);
    update({ vocabulary: words });
    try {
      await fetch(API("/vocabulary"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ words }),
      });
    } catch {
      // non-fatal
    }
    setVocabSaved(true);
    setTimeout(() => setVocabSaved(false), 2000);
  }

  async function setAudioSetting(patch: {
    agc?: boolean;
    noise_reduction?: boolean;
  }) {
    try {
      await fetch(API("/settings/audio"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
    } catch {
      // non-fatal
    }
  }

  async function addSnippet() {
    const trigger = snippetTrigger.trim();
    const expansion = snippetExpansion.trim();
    if (!trigger || !expansion) return;
    try {
      await fetch(API("/snippets"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trigger, expansion }),
      });
      setSnippets((prev) => ({ ...prev, [trigger]: expansion }));
      setSnippetTrigger("");
      setSnippetExpansion("");
    } catch {
      // non-fatal
    }
  }

  async function deleteSnippet(trigger: string) {
    try {
      await fetch(API(`/snippets/${encodeURIComponent(trigger)}`), {
        method: "DELETE",
      });
      setSnippets((prev) => {
        const next = { ...prev };
        delete next[trigger];
        return next;
      });
    } catch {
      // non-fatal
    }
  }

  const defaultDeviceIndex = devices.find((d) => d.default)?.index ?? -1;
  const selectedDevice =
    settings.audioDeviceIndex >= 0
      ? settings.audioDeviceIndex
      : defaultDeviceIndex;

  return (
    <div style={ROOT}>
      <div style={CONTAINER}>
        <h1 style={PAGE_TITLE}>Vox Settings</h1>

        {/* LLM Section */}
        <div style={SECTION}>
          <div style={SECTION_LABEL}>LLM Post-Processing</div>

          <label style={TOGGLE_LABEL}>
            <input
              type="checkbox"
              checked={settings.useLlm}
              onChange={(e) => update({ useLlm: e.target.checked })}
            />
            Use local LLM to clean transcripts
          </label>
          <p style={HINT}>
            Runs qwen2.5-3b locally. Disable for raw Whisper output.
          </p>

          {settings.useLlm && (
            <>
              <label style={{ ...TOGGLE_LABEL, marginTop: 12 }}>
                <input
                  type="checkbox"
                  checked={settings.useGroq}
                  onChange={(e) => update({ useGroq: e.target.checked })}
                />
                Use Groq API instead of local LLM
              </label>

              {settings.hasGroqKey && !settings.useGroq && (
                <span style={BADGE_OK}>Groq key saved ✓</span>
              )}
              {settings.hasGroqKey && settings.useGroq && (
                <p style={{ ...HINT, color: "#16a34a" }}>
                  Using Groq API ✓ — requests sent to cloud
                </p>
              )}

              {(!settings.hasGroqKey || settings.useGroq) && (
                <div style={{ marginTop: 12 }}>
                  <div style={ROW}>
                    <input
                      type="password"
                      placeholder="gsk_..."
                      value={groqKeyInput}
                      onChange={(e) => setGroqKeyInput(e.target.value)}
                      style={INPUT}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") saveGroqKey();
                      }}
                    />
                    <button
                      style={
                        groqStatus === "testing" || !groqKeyInput.trim()
                          ? BTN_DISABLED
                          : BTN
                      }
                      disabled={
                        groqStatus === "testing" || !groqKeyInput.trim()
                      }
                      onClick={saveGroqKey}
                    >
                      {groqStatus === "testing" ? "Testing…" : "Test & Save"}
                    </button>
                  </div>
                  {groqStatus === "ok" && (
                    <span style={BADGE_OK}>Key validated and saved ✓</span>
                  )}
                  {groqStatus === "error" && (
                    <span style={BADGE_ERR}>{groqError}</span>
                  )}
                </div>
              )}
            </>
          )}
        </div>

        {/* Audio Device Section */}
        <div style={SECTION}>
          <div style={SECTION_LABEL}>Audio Input Device</div>
          {!portReady ? (
            <p style={HINT}>Connecting to sidecar…</p>
          ) : devices.length === 0 ? (
            <p style={HINT}>No devices found. Check microphone permissions.</p>
          ) : (
            <select
              style={SELECT}
              value={selectedDevice}
              onChange={(e) =>
                update({ audioDeviceIndex: Number(e.target.value) })
              }
            >
              {devices.map((d) => (
                <option key={d.index} value={d.index}>
                  {d.name}
                  {d.default ? " (default)" : ""}
                </option>
              ))}
            </select>
          )}
          {devices.find((d) => d.index === selectedDevice)
            ?.bluetooth_warning && (
            <p style={{ color: "#b45309", fontSize: 12, marginTop: 4 }}>
              Bluetooth mics add 80–150 ms latency and may clip recording
              starts.
            </p>
          )}
          <p style={HINT}>Takes effect on the next recording session.</p>
        </div>

        {/* Hotkey Section */}
        <div style={SECTION}>
          <div style={SECTION_LABEL}>Global Hotkey</div>
          <div
            style={{
              display: "inline-flex",
              gap: 4,
              alignItems: "center",
            }}
          >
            {["Ctrl", "Shift", "Space"].map((k) => (
              <kbd
                key={k}
                style={{
                  padding: "3px 8px",
                  border: "1px solid #ccc",
                  borderRadius: 4,
                  background: "#f5f5f5",
                  fontSize: 13,
                  fontFamily: "inherit",
                }}
              >
                {k}
              </kbd>
            ))}
          </div>
          <p style={HINT}>Hold to record, release to transcribe and inject.</p>
        </div>

        {/* Vocabulary Section */}
        <div style={SECTION}>
          <div style={SECTION_LABEL}>Custom Vocabulary</div>
          <textarea
            style={TEXTAREA}
            value={vocabText}
            onChange={(e) => setVocabText(e.target.value)}
            placeholder={
              "Kubernetes\nPyTorch\nTauri\n(one word or phrase per line)"
            }
          />
          <div
            style={{
              marginTop: 8,
              display: "flex",
              alignItems: "center",
              gap: 12,
            }}
          >
            <button style={BTN} onClick={saveVocabulary}>
              Save
            </button>
            {vocabSaved && <span style={BADGE_SAVED}>Saved ✓</span>}
          </div>
          <p style={HINT}>
            Words injected as hints into the transcription model. Takes effect
            on the next session.
          </p>
        </div>

        {/* Writing Style Section */}
        <div style={SECTION}>
          <div style={SECTION_LABEL}>Writing Style</div>
          <select
            style={SELECT}
            value={settings.defaultStyle}
            onChange={(e) => update({ defaultStyle: e.target.value })}
          >
            <option value="auto">Auto (match app context)</option>
            <option value="casual">Casual</option>
            <option value="concise">Concise</option>
            <option value="professional">Professional</option>
            <option value="technical">Technical</option>
            <option value="code">Code</option>
          </select>
          <p style={HINT}>
            Applied by the LLM when cleaning transcripts. Auto uses the detected
            app profile.
          </p>
        </div>

        {/* Audio Processing Section */}
        <div style={SECTION}>
          <div style={SECTION_LABEL}>Audio Processing</div>
          <label style={TOGGLE_LABEL}>
            <input
              type="checkbox"
              defaultChecked={true}
              onChange={(e) => setAudioSetting({ agc: e.target.checked })}
            />
            Automatic Gain Control
          </label>
          <label style={{ ...TOGGLE_LABEL, marginTop: 8 }}>
            <input
              type="checkbox"
              defaultChecked={false}
              onChange={(e) =>
                setAudioSetting({ noise_reduction: e.target.checked })
              }
            />
            Noise Reduction (experimental)
          </label>
          <p style={HINT}>
            Changes take effect immediately on the next audio chunk.
          </p>
        </div>

        {/* Snippets Section */}
        <div style={SECTION}>
          <div style={SECTION_LABEL}>Text Snippets</div>
          <div style={ROW}>
            <input
              style={INPUT}
              placeholder="trigger"
              value={snippetTrigger}
              onChange={(e) => setSnippetTrigger(e.target.value)}
            />
            <input
              style={INPUT}
              placeholder="expansion"
              value={snippetExpansion}
              onChange={(e) => setSnippetExpansion(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") addSnippet();
              }}
            />
            <button
              style={
                !snippetTrigger.trim() || !snippetExpansion.trim()
                  ? BTN_DISABLED
                  : BTN
              }
              disabled={!snippetTrigger.trim() || !snippetExpansion.trim()}
              onClick={addSnippet}
            >
              Add
            </button>
          </div>
          {Object.entries(snippets).map(([trigger, expansion]) => (
            <div
              key={trigger}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                marginTop: 6,
                fontSize: 13,
              }}
            >
              <code
                style={{
                  background: "#f0f0f0",
                  padding: "2px 6px",
                  borderRadius: 4,
                  flexShrink: 0,
                }}
              >
                {trigger}
              </code>
              <span style={{ color: "#666", flexShrink: 0 }}>→</span>
              <span style={{ flex: 1, color: "#1a1a1a" }}>{expansion}</span>
              <button
                style={{
                  border: "none",
                  background: "none",
                  color: "#dc2626",
                  cursor: "pointer",
                  fontSize: 13,
                  padding: "0 4px",
                  flexShrink: 0,
                }}
                onClick={() => deleteSnippet(trigger)}
              >
                Remove
              </button>
            </div>
          ))}
          <p style={HINT}>
            Spoken triggers are replaced with their expansion after LLM cleanup.
          </p>
        </div>

        {/* Data Collection Section */}
        <div style={SECTION}>
          <div style={SECTION_LABEL}>Passive Data Collection</div>
          <label style={TOGGLE_LABEL}>
            <input
              type="checkbox"
              checked={settings.passiveCollectionEnabled}
              onChange={(e) => togglePassiveCollection(e.target.checked)}
            />
            Log transcripts for fine-tuning (stored locally)
          </label>
          <p style={HINT}>
            Transcripts are saved to your local data directory only. Never
            transmitted. Used to fine-tune the model from the Fine-Tuning
            dashboard.
          </p>
        </div>

        {/* Privacy Section */}
        <div style={SECTION_LAST}>
          <div style={SECTION_LABEL}>Privacy</div>
          <div style={ROW}>
            <label style={{ fontSize: 14, flexShrink: 0 }}>Log retention</label>
            <select
              style={{ ...SELECT, flex: "0 0 auto", width: 160 }}
              value={settings.retentionDays ?? ""}
              onChange={(e) => {
                const val =
                  e.target.value === ""
                    ? null
                    : (Number(e.target.value) as 0 | 7 | 30);
                update({ retentionDays: val });
                fetch(API("/settings/privacy"), {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ retention_days: val }),
                }).catch(() => {});
              }}
            >
              <option value="">Keep forever</option>
              <option value="30">30 days</option>
              <option value="7">7 days</option>
              <option value="0">Disable collection</option>
            </select>
          </div>
          <p style={HINT}>
            Old log entries and audio clips are deleted on sidecar startup.
          </p>
        </div>
      </div>
    </div>
  );
}
