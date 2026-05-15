import { useState } from "react";
import type { Persona } from "../api";

export type ConsentSelection =
  | { kind: "solo" }
  | { kind: "partner_present"; partner_label: string };

interface Props {
  persona: Persona;
  onConfirm: (selection: ConsentSelection) => void;
  onCancel: () => void;
}

/**
 * Pre-mic consent gate ("Who is here?").  The mic does not open until
 * the user explicitly picks solo OR confirms partner-present + the
 * partner is reading along.  Maps onto Sprint 2 DoD:
 *
 *   Consent flow: pre-mic "Who is here?" screen → if partner selected,
 *   both-voices consent OR shared-screen tap → persists
 *   conversation_consent_events rows atomically before mic opens.
 */
export function ConsentGate({ persona, onConfirm, onCancel }: Props) {
  const [mode, setMode] = useState<"solo" | "partner" | null>(null);
  const [partnerLabel, setPartnerLabel] = useState("");
  const [partnerAck, setPartnerAck] = useState(false);

  const canConfirm =
    mode === "solo" || (mode === "partner" && partnerLabel.trim().length > 0 && partnerAck);

  function submit() {
    if (mode === "solo") {
      onConfirm({ kind: "solo" });
    } else if (mode === "partner" && canConfirm) {
      onConfirm({ kind: "partner_present", partner_label: partnerLabel.trim() });
    }
  }

  return (
    <section className="mx-auto max-w-2xl px-6 py-10">
      <header className="mb-6">
        <p className="text-xs uppercase tracking-widest text-veas-muted">
          Before we open the mic
        </p>
        <h2 className="text-xl font-semibold text-white">
          Who's here for this conversation with {persona.display_name}?
        </h2>
        <p className="mt-2 text-sm text-veas-muted">
          The mic stays closed until you tell us. Your audio is transcribed in real-time
          and the raw recording is discarded — only the text is kept.
        </p>
      </header>

      <div className="space-y-3">
        <button
          type="button"
          onClick={() => setMode("solo")}
          className={`w-full rounded-md border p-4 text-left transition ${
            mode === "solo"
              ? "border-veas-accent bg-veas-accent/10"
              : "border-white/10 bg-veas-surface hover:border-white/20"
          }`}
        >
          <h3 className="text-sm font-medium text-white">Just me</h3>
          <p className="mt-1 text-xs text-veas-muted">
            Only my voice will be captured.
          </p>
        </button>

        <button
          type="button"
          onClick={() => setMode("partner")}
          className={`w-full rounded-md border p-4 text-left transition ${
            mode === "partner"
              ? "border-veas-accent bg-veas-accent/10"
              : "border-white/10 bg-veas-surface hover:border-white/20"
          }`}
        >
          <h3 className="text-sm font-medium text-white">Me and a partner</h3>
          <p className="mt-1 text-xs text-veas-muted">
            We both speak. Both voices will be captured and labeled.
          </p>
        </button>

        {mode === "partner" && (
          <div className="rounded-md border border-white/10 bg-veas-bg/40 p-4">
            <label className="block text-sm text-white">
              How should we label your partner in the transcript?
              <input
                type="text"
                value={partnerLabel}
                onChange={(e) => setPartnerLabel(e.target.value)}
                placeholder="e.g. Sam"
                className="mt-2 w-full rounded-md border border-white/10 bg-veas-bg/60 px-3 py-2 text-sm text-white placeholder:text-veas-muted focus:border-veas-accent focus:outline-none focus:ring-1 focus:ring-veas-accent/60"
              />
            </label>
            <label className="mt-3 flex items-start gap-2 text-xs text-white">
              <input
                type="checkbox"
                checked={partnerAck}
                onChange={(e) => setPartnerAck(e.target.checked)}
                className="mt-0.5 h-4 w-4 rounded border-white/20 bg-veas-bg text-veas-accent focus:ring-veas-accent/60"
              />
              <span>
                I confirm my partner is in the room and aware their voice will be
                captured and transcribed. Either of us can hit Stop for everyone at
                any time.
              </span>
            </label>
          </div>
        )}
      </div>

      <div className="mt-8 flex items-center justify-end gap-3">
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md px-4 py-2 text-sm text-veas-muted hover:text-white"
        >
          Back
        </button>
        <button
          type="button"
          onClick={submit}
          disabled={!canConfirm}
          className="rounded-md bg-veas-accent px-5 py-2 text-sm font-medium text-veas-bg transition hover:bg-veas-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Open the mic
        </button>
      </div>
    </section>
  );
}
