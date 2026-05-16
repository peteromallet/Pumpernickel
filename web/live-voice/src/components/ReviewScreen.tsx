import { useState } from "react";
import {
  saveReview,
  type Persona,
  type ReviewItem,
  type ReviewNote,
  type SessionReview,
} from "../api";

interface Props {
  persona: Persona;
  review: SessionReview;
  onSaved: () => void;
  onDiscard: () => void;
}

/**
 * Post-session review screen.  Four sections (per
 * docs/live-conversation-mode.md §UI):
 *
 *   * What Rosi heard      — primary user transcript bullets
 *   * What you decided     — covered agenda items
 *   * Still open           — pending/active items
 *   * What Rosi remembers  — conversation_notes
 *
 * Covered items and notes are editable inline.  Save persists edits
 * through `POST /api/live/sessions/{id}/review/save`; Discard skips the
 * write-through but keeps the transcript + conversation row.
 */
export function ReviewScreen({ persona, review, onSaved, onDiscard }: Props) {
  const [items, setItems] = useState<ReviewItem[]>(review.what_decided);
  const [notes, setNotes] = useState<ReviewNote[]>(review.what_to_remember);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function patchItem(i: number, summary: string) {
    setItems((prev) => prev.map((it, idx) => (idx === i ? { ...it, summary } : it)));
  }

  function patchNote(i: number, text: string) {
    setNotes((prev) => prev.map((n, idx) => (idx === i ? { ...n, text } : n)));
  }

  function dropNote(i: number) {
    setNotes((prev) => prev.map((n, idx) => (idx === i ? { ...n, text: "" } : n)));
  }

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      await saveReview(review.session_id, {
        keep_items: items.map((it) => ({
          item_id: it.item_id,
          summary: it.summary || undefined,
        })),
        keep_notes: notes.map((n) => ({ note_id: n.note_id, text: n.text })),
      });
      onSaved();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Save failed.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="mx-auto max-w-2xl px-6 py-10">
      <header className="mb-6">
        <p className="text-xs uppercase tracking-widest text-veas-muted">
          Review with {persona.display_name}
        </p>
        <h2 className="text-xl font-semibold text-white">
          Before we close out — anything to keep or fix?
        </h2>
        <p className="mt-2 text-sm text-veas-muted">
          Edit anything that's off. Hit Save and we'll remember it. Hit Discard
          and we'll keep the transcript only.
        </p>
      </header>

      {review.is_empty && (
        <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-amber-200">
          The session ended before any user or bot turns landed — nothing to review.
        </div>
      )}

      <Section title="What I heard from you">
        {review.what_heard.length === 0 ? (
          <p className="text-xs text-veas-muted">(no user turns)</p>
        ) : (
          <ul className="space-y-1 text-sm text-white/90">
            {review.what_heard.map((line, i) => (
              <li key={i}>• {line}</li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="What you decided">
        {items.length === 0 ? (
          <p className="text-xs text-veas-muted">(no items covered)</p>
        ) : (
          <ul className="space-y-3">
            {items.map((it, i) => (
              <li
                key={it.item_id}
                className="rounded-md border border-white/5 bg-veas-bg/40 p-3"
              >
                <h4 className="text-sm font-medium text-white">{it.title}</h4>
                {it.evidence_quote && (
                  <p className="mt-1 text-xs italic text-veas-muted">
                    "{it.evidence_quote}"
                  </p>
                )}
                <textarea
                  value={it.summary || ""}
                  onChange={(e) => patchItem(i, e.target.value)}
                  rows={2}
                  className="mt-2 w-full rounded-md border border-white/10 bg-veas-bg/60 px-3 py-2 text-xs text-white"
                />
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="Still open">
        {review.still_open.length === 0 ? (
          <p className="text-xs text-veas-muted">(nothing left unhandled)</p>
        ) : (
          <ul className="space-y-2 text-sm">
            {review.still_open.map((it) => (
              <li
                key={it.item_id}
                className="rounded-md border border-white/5 bg-veas-bg/40 p-3"
              >
                <p className="text-white">{it.title}</p>
                {it.intent && (
                  <p className="mt-1 text-xs text-veas-muted">{it.intent}</p>
                )}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="What I should remember">
        {notes.length === 0 ? (
          <p className="text-xs text-veas-muted">(no notes captured)</p>
        ) : (
          <ul className="space-y-2">
            {notes.map((n, i) => (
              <li
                key={n.note_id}
                className="flex items-start gap-2 rounded-md border border-white/5 bg-veas-bg/40 p-3"
              >
                <span className="rounded-full border border-white/10 px-2 py-0.5 text-[10px] uppercase tracking-wider text-veas-muted">
                  {n.kind}
                </span>
                <textarea
                  value={n.text}
                  onChange={(e) => patchNote(i, e.target.value)}
                  rows={2}
                  className="flex-1 rounded-md border border-white/10 bg-veas-bg/60 px-3 py-2 text-xs text-white"
                />
                <button
                  type="button"
                  onClick={() => dropNote(i)}
                  className="text-xs text-rose-300 hover:text-rose-200"
                >
                  drop
                </button>
              </li>
            ))}
          </ul>
        )}
      </Section>

      {error && (
        <p className="mt-4 text-sm text-rose-300">Save failed: {error}</p>
      )}

      <div className="mt-8 flex items-center justify-end gap-3">
        <button
          type="button"
          onClick={onDiscard}
          disabled={saving}
          className="rounded-md px-4 py-2 text-sm text-veas-muted hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
        >
          Discard
        </button>
        <button
          type="button"
          onClick={handleSave}
          disabled={saving}
          className="rounded-md bg-veas-accent px-5 py-2 text-sm font-medium text-veas-bg hover:bg-veas-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {saving ? "Saving…" : "Save"}
        </button>
      </div>
    </section>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mt-6">
      <h3 className="mb-2 text-xs uppercase tracking-widest text-veas-muted">
        {title}
      </h3>
      <div className="rounded-lg border border-white/5 bg-veas-surface p-4">
        {children}
      </div>
    </div>
  );
}
