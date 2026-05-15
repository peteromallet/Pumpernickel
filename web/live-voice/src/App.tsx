import { useState } from "react";
import { Header } from "./components/Header";
import { PersonaPicker } from "./components/PersonaPicker";
import { SessionCard } from "./components/SessionCard";
import { LiveScreen } from "./components/LiveScreen";
import type { Persona } from "./api";

type View =
  | { kind: "picker" }
  | { kind: "session"; persona: Persona }
  | { kind: "live"; persona: Persona; sessionId: string };

export default function App() {
  const [view, setView] = useState<View>({ kind: "picker" });

  return (
    <div className="min-h-screen bg-veas-bg text-slate-100">
      <Header />
      <main>
        {view.kind === "picker" && (
          <PersonaPicker
            onPick={(persona) => setView({ kind: "session", persona })}
          />
        )}
        {view.kind === "session" && (
          <SessionCard
            persona={view.persona}
            onCancel={() => setView({ kind: "picker" })}
            onStarted={(sessionId) =>
              setView({ kind: "live", persona: view.persona, sessionId })
            }
          />
        )}
        {view.kind === "live" && (
          <LiveScreen
            persona={view.persona}
            sessionId={view.sessionId}
            onEnd={() => setView({ kind: "picker" })}
          />
        )}
      </main>
      <footer className="mx-auto max-w-5xl px-6 py-6 text-center text-xs text-veas-muted">
        Veas mediator · Live Voice Agent
      </footer>
    </div>
  );
}
