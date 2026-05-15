import { useEffect, useState } from "react";
import { fetchPersonas, LiveApiError, type Persona } from "../api";

interface Props {
  onPick: (persona: Persona) => void;
}

export function PersonaPicker({ onPick }: Props) {
  const [personas, setPersonas] = useState<Persona[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchPersonas()
      .then((p) => {
        if (cancelled) return;
        setPersonas(p);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof LiveApiError) {
          setError(err.message);
        } else {
          setError("Could not load personas. Try again in a moment.");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <section className="mx-auto max-w-5xl px-6 py-10">
      <div className="mb-6">
        <h2 className="text-2xl font-semibold text-white">Pick a persona</h2>
        <p className="mt-1 text-sm text-veas-muted">
          Choose who you'd like to talk with. Each persona has its own style and
          memory.
        </p>
      </div>

      {loading && (
        <div className="rounded-lg border border-white/5 bg-veas-surface/40 p-6 text-veas-muted">
          Loading personas…
        </div>
      )}

      {error && !loading && (
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-amber-200">
          {error}
        </div>
      )}

      {!loading && !error && personas && personas.length === 0 && (
        <div className="rounded-lg border border-white/5 bg-veas-surface/40 p-6 text-veas-muted">
          No personas are configured yet.
        </div>
      )}

      {!loading && !error && personas && personas.length > 0 && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {personas.map((p) => (
            <article
              key={p.bot_id}
              className="flex flex-col rounded-lg border border-white/5 bg-veas-surface p-5 shadow-sm transition hover:border-veas-accent/40"
            >
              <h3 className="text-lg font-semibold text-white">
                {p.display_name}
              </h3>
              {p.description && (
                <p className="mt-2 flex-1 text-sm text-veas-muted">
                  {p.description}
                </p>
              )}
              <button
                type="button"
                onClick={() => onPick(p)}
                className="mt-4 inline-flex items-center justify-center rounded-md bg-veas-accent px-4 py-2 text-sm font-medium text-veas-bg transition hover:bg-veas-accent/90 focus:outline-none focus:ring-2 focus:ring-veas-accent/60"
              >
                Start a conversation
              </button>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
