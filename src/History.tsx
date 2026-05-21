import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";

// ── Port discovery (same pattern as Settings) ─────────────────────────────────

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

// ── Types ─────────────────────────────────────────────────────────────────────

interface HistoryItem {
  timestamp?: string;
  raw_asr?: string;
  user_edited?: string;
  profile?: string;
  executable_name?: string;
  final_pass_ms?: number;
  llm_ms?: number;
  audio_file?: string;
  corrected?: boolean;
}

interface HistoryResponse {
  items: HistoryItem[];
  total_in_log: number;
}

// ── Styles ────────────────────────────────────────────────────────────────────

const ROOT: React.CSSProperties = {
  fontFamily: "system-ui, -apple-system, sans-serif",
  fontSize: 14,
  color: "#1a1a1a",
  background: "#fff",
  // index.html sets html/body/#root { overflow: hidden } for the transparent
  // main overlay — secondary windows must override on their own root or
  // long content gets clipped past the window height.
  height: "100vh",
  overflowY: "auto",
  padding: 0,
  margin: 0,
};

const CONTAINER: React.CSSProperties = {
  maxWidth: 760,
  margin: "0 auto",
  padding: "20px 20px 40px",
};

const TITLE: React.CSSProperties = {
  fontSize: 20,
  fontWeight: 600,
  margin: "0 0 16px",
};

const SEARCH: React.CSSProperties = {
  width: "100%",
  padding: "8px 12px",
  border: "1px solid #ddd",
  borderRadius: 6,
  fontSize: 14,
  marginBottom: 12,
  boxSizing: "border-box",
};

const ITEM: React.CSSProperties = {
  padding: "10px 12px",
  border: "1px solid #eee",
  borderRadius: 8,
  marginBottom: 8,
  background: "#fafafa",
};

const META: React.CSSProperties = {
  fontSize: 12,
  color: "#888",
  display: "flex",
  gap: 12,
  marginBottom: 6,
  flexWrap: "wrap",
};

const TEXT: React.CSSProperties = {
  fontSize: 14,
  color: "#1a1a1a",
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
};

const ACTIONS: React.CSSProperties = {
  marginTop: 8,
  display: "flex",
  gap: 8,
};

const BTN: React.CSSProperties = {
  padding: "5px 10px",
  fontSize: 12,
  border: "1px solid #ccc",
  borderRadius: 4,
  background: "#fff",
  cursor: "pointer",
};

const FOOTER: React.CSSProperties = {
  fontSize: 12,
  color: "#888",
  marginTop: 12,
  textAlign: "right" as const,
};

// ── Component ─────────────────────────────────────────────────────────────────

export default function History() {
  const [items, setItems] = useState<HistoryItem[]>([]);
  const [totalInLog, setTotalInLog] = useState(0);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const fetchHistory = useCallback(async (q: string) => {
    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams();
      if (q.trim()) params.set("q", q.trim());
      params.set("limit", "200");
      const resp = await fetch(API(`/history?${params.toString()}`), {
        signal: AbortSignal.timeout(5000),
      });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const data = (await resp.json()) as HistoryResponse;
      setItems(data.items);
      setTotalInLog(data.total_in_log);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    discoverSidecarPort().then(() => fetchHistory(""));
  }, [fetchHistory]);

  // Debounced search
  useEffect(() => {
    const id = setTimeout(() => fetchHistory(query), 250);
    return () => clearTimeout(id);
  }, [query, fetchHistory]);

  const reInject = async (text: string) => {
    try {
      await invoke("inject_text", { text });
    } catch (e) {
      console.error("re-inject failed:", e);
    }
  };

  const copyToClipboard = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      console.error("clipboard write failed:", e);
    }
  };

  return (
    <div style={ROOT}>
      <div style={CONTAINER}>
        <h1 style={TITLE}>Dictation history</h1>
        <input
          type="text"
          style={SEARCH}
          placeholder="Search transcripts, app, or original ASR…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />

        {error && (
          <div style={{ color: "#dc2626", fontSize: 13, marginBottom: 8 }}>
            {error}
          </div>
        )}

        {loading && items.length === 0 ? (
          <div style={{ color: "#888", fontSize: 13 }}>Loading…</div>
        ) : items.length === 0 ? (
          <div style={{ color: "#888", fontSize: 13 }}>
            {totalInLog === 0
              ? "No dictations logged yet. Enable Passive Data Collection in Settings to start recording history."
              : "No entries match your search."}
          </div>
        ) : (
          items.map((item, idx) => {
            const text = item.user_edited ?? item.raw_asr ?? "";
            const when = item.timestamp
              ? new Date(item.timestamp).toLocaleString()
              : "";
            const ms = (item.final_pass_ms ?? 0) + (item.llm_ms ?? 0);
            return (
              <div key={idx} style={ITEM}>
                <div style={META}>
                  {when && <span>{when}</span>}
                  {item.executable_name && <span>{item.executable_name}</span>}
                  {item.profile && <span>{item.profile}</span>}
                  {ms > 0 && <span>{ms} ms</span>}
                  {item.corrected && (
                    <span style={{ color: "#16a34a" }}>edited</span>
                  )}
                  {item.audio_file && <span>audio saved</span>}
                </div>
                <div style={TEXT}>{text}</div>
                {item.raw_asr &&
                  item.user_edited &&
                  item.raw_asr !== item.user_edited && (
                    <div
                      style={{
                        ...TEXT,
                        color: "#888",
                        fontStyle: "italic",
                        marginTop: 4,
                        fontSize: 12,
                      }}
                    >
                      raw: {item.raw_asr}
                    </div>
                  )}
                <div style={ACTIONS}>
                  <button style={BTN} onClick={() => copyToClipboard(text)}>
                    Copy
                  </button>
                  <button style={BTN} onClick={() => reInject(text)}>
                    Re-inject
                  </button>
                </div>
              </div>
            );
          })
        )}

        <div style={FOOTER}>
          {totalInLog} total entries · showing {items.length}
        </div>
      </div>
    </div>
  );
}
