import { useEffect, useState } from "react";
import { listen } from "@tauri-apps/api/event";

type LoadState = "waiting" | "ready" | "degraded";

export default function App() {
  const [loadState, setLoadState] = useState<LoadState>("waiting");
  const [missing, setMissing] = useState<string[]>([]);

  useEffect(() => {
    const cleanups: Array<() => void> = [];

    const reg = async () => {
      cleanups.push(
        await listen<null>("models-ready", () => {
          setLoadState("ready");
        })
      );
      cleanups.push(
        await listen<{ missing: string[] }>("sidecar-degraded", (e) => {
          setMissing(e.payload.missing);
          setLoadState("degraded");
        })
      );
    };

    reg();
    return () => cleanups.forEach((fn) => fn());
  }, []);

  return (
    <div
      style={{
        fontFamily: "system-ui, sans-serif",
        padding: "12px 16px",
        color: "#fff",
        background: "rgba(20,20,20,0.85)",
        borderRadius: 8,
        fontSize: 14,
      }}
    >
      {loadState === "waiting" && <p style={{ margin: 0 }}>Connecting to Vox...</p>}
      {loadState === "ready" && <p style={{ margin: 0 }}>Ready</p>}
      {loadState === "degraded" && (
        <p style={{ margin: 0, color: "#f87171" }}>
          Degraded — missing: {missing.join(", ")}
        </p>
      )}
    </div>
  );
}
