import { useEffect, useRef, useState } from "react";
import { liveSocketUrl, type Persona } from "../api";

interface Props {
  persona: Persona;
  sessionId: string;
  onEnd: () => void;
}

interface PhaseEvent {
  ts: number;
  text: string;
}

export function LiveScreen({ persona, sessionId, onEnd }: Props) {
  const [events, setEvents] = useState<PhaseEvent[]>([]);
  const [status, setStatus] = useState<"connecting" | "open" | "closed" | "error">(
    "connecting",
  );
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    const ws = new WebSocket(liveSocketUrl(sessionId));
    wsRef.current = ws;

    ws.onopen = () => setStatus("open");
    ws.onclose = () => setStatus((s) => (s === "error" ? s : "closed"));
    ws.onerror = () => setStatus("error");
    ws.onmessage = (msg) => {
      let text: string;
      try {
        const parsed = JSON.parse(msg.data);
        text =
          typeof parsed === "string"
            ? parsed
            : (parsed.phase ?? parsed.text ?? JSON.stringify(parsed));
      } catch {
        text = String(msg.data);
      }
      setEvents((prev) => [...prev, { ts: Date.now(), text }]);
    };

    return () => {
      try {
        ws.close();
      } catch {
        // ignore
      }
      wsRef.current = null;
    };
  }, [sessionId]);

  function handleEnd() {
    try {
      wsRef.current?.close();
    } catch {
      // ignore
    }
    onEnd();
  }

  const statusLabel: Record<typeof status, string> = {
    connecting: "Connecting…",
    open: "Live",
    closed: "Disconnected",
    error: "Connection error",
  };
  const statusColor: Record<typeof status, string> = {
    connecting: "bg-amber-400",
    open: "bg-emerald-400",
    closed: "bg-slate-500",
    error: "bg-rose-500",
  };

  return (
    <section className="mx-auto max-w-2xl px-6 py-10">
      <div className="rounded-lg border border-white/5 bg-veas-surface p-6">
        <header className="mb-6 flex items-center justify-between">
          <div>
            <p className="text-xs uppercase tracking-widest text-veas-muted">
              In session with
            </p>
            <h2 className="text-xl font-semibold text-white">
              {persona.display_name}
            </h2>
          </div>
          <span className="inline-flex items-center gap-2 rounded-full bg-white/5 px-3 py-1 text-xs text-white">
            <span
              className={`h-2 w-2 rounded-full ${statusColor[status]}`}
              aria-hidden
            />
            {statusLabel[status]}
          </span>
        </header>

        <div className="rounded-md border border-white/5 bg-veas-bg/60 px-4 py-6 text-center text-sm text-veas-muted">
          Live voice mode is starting up…
        </div>

        <div className="mt-6">
          <h3 className="text-xs uppercase tracking-widest text-veas-muted">
            Transcript
          </h3>
          <div className="mt-2 max-h-72 min-h-[8rem] overflow-y-auto rounded-md border border-white/5 bg-veas-bg/40 p-3 text-sm">
            {events.length === 0 ? (
              <p className="text-veas-muted">Waiting for events…</p>
            ) : (
              <ul className="space-y-2">
                {events.map((e, i) => (
                  <li key={i} className="font-mono text-xs text-slate-200">
                    <span className="text-veas-muted">
                      {new Date(e.ts).toLocaleTimeString()}
                    </span>{" "}
                    {e.text}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        <div className="mt-6 flex justify-end">
          <button
            type="button"
            onClick={handleEnd}
            className="rounded-md bg-rose-500/90 px-4 py-2 text-sm font-medium text-white transition hover:bg-rose-500"
          >
            End session
          </button>
        </div>

        <p className="mt-4 text-[11px] text-veas-muted">
          Session id: <span className="font-mono">{sessionId}</span>
        </p>
      </div>
    </section>
  );
}
