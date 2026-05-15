export interface Persona {
  bot_id: string;
  display_name: string;
  description?: string;
}

export interface CreateSessionRequest {
  bot_id: string;
  steering_text: string;
  mode: "open_ended" | "guided";
}

export interface CreateSessionResponse {
  session_id: string;
}

export class LiveApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function handle<T>(res: Response): Promise<T> {
  if (res.status === 503) {
    throw new LiveApiError(
      "Live conversations are not yet available on this deployment.",
      503,
    );
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body && typeof body === "object" && "detail" in body) {
        detail = String((body as { detail: unknown }).detail);
      }
    } catch {
      // ignore
    }
    throw new LiveApiError(detail, res.status);
  }
  return (await res.json()) as T;
}

export async function fetchPersonas(): Promise<Persona[]> {
  const res = await fetch("/api/live/personas", {
    headers: { Accept: "application/json" },
  });
  const data = await handle<{ personas?: Persona[] } | Persona[]>(res);
  if (Array.isArray(data)) return data;
  return data.personas ?? [];
}

export async function createSession(
  req: CreateSessionRequest,
): Promise<CreateSessionResponse> {
  const res = await fetch("/api/live/sessions", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify(req),
  });
  return handle<CreateSessionResponse>(res);
}

export function liveSocketUrl(sessionId: string): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ws/live/${encodeURIComponent(sessionId)}`;
}
