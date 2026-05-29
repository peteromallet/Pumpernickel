# Sprint 1 — Live-Voice Identity & Discovery

Part 1 of the Discord-authored-agendas epic. Companion design context:
`megaplans/discord-agenda-authoring-brief.md`. This sprint builds the foundation
Sprint 2 depends on; it does **not** build any Discord tools.

## Outcome

Make a live-voice conversation owned by, and discoverable to, the **real
authenticated user** — so a conversation created for user X is visible and
startable only by X (or their partner), and the web app can list "my
conversations" on entry. Today the live-voice surface operates as a fixed test
user and has no per-user listing, which makes anything an agent creates
undiscoverable.

## Scope

IN:
- Replace the fixed-test-user identity (`_resolve_test_user_id`,
  `app/routers/live_voice.py:134`) on the live-voice surface with the **real
  authenticated `mediator.users.id`** for every entry point that creates or reads
  a conversation.
- **Ownership enforcement** on `GET /sessions/{id}/card`, `get_session`, the
  `/ws/live/{id}` WebSocket, `/end`, `/review/save`, and prep/debrief retry — a
  caller may only act on a conversation where `user_id` or `partner_user_id`
  matches the authenticated user. Cross-user access returns 403/404.
- A new **user-scoped list endpoint** `GET /api/live/sessions` returning the
  authenticated user's conversations (id, status, topic label, prep_summary /
  steering_text, item count, created_at), filterable by status, newest first.
- **Frontend discovery**: the live-voice web app loads that list on entry and
  shows ready/waiting conversations the user can resume/start
  (`web/live-voice/src/App.tsx`, `api.ts`, `SessionCard.tsx`).
- Tests covering ownership rejection, list scoping, and the create→discover→start
  round trip.

OUT (anti-scope):
- The Discord agenda-authoring tools, markdown converter, hot-context section —
  all Sprint 2.
- Any change to turn-loop, prep, or synthesis *logic* beyond adding ownership
  checks.
- Broad auth redesign — magic-link (`auth_magic_link.py`) stays as the auth
  mechanism; this sprint consumes it, it does not replace it.
- Discard/cancel of conversations, reading completed-conversation outcomes
  (later, per the design brief).

## Locked decisions

- The live-voice surface keys ownership off the authenticated user, not a fixed
  test UUID.
- Ownership is enforced for read AND mutate paths, including the WebSocket (which
  today verifies a JWT at `live_voice.py:1535` but never compares it to
  `conversation.user_id`).
- A user-scoped list endpoint is the discovery mechanism (not RLS-driven client
  queries).

## Open questions (resolve in prep — do not invent answers)

1. **How is the live-voice web client authenticated today on the HTTP paths?**
   The WS handler verifies a JWT; do the `POST /sessions` / `/card` HTTP routes
   carry the same Supabase JWT, or are they currently unauthenticated behind the
   test-user shim? Map the real request auth before designing the dependency.
2. **Enforcement mechanism: app-level explicit checks vs. user-JWT + RLS.** The
   app currently reads via a **service-role pool** (`app/db.py`) that bypasses
   RLS, with explicit ownership checks in tools. Recommended default: keep the
   service-role pool and add **explicit app-level ownership checks** (matches the
   existing tool pattern, e.g. `write_tools.add_memory`), rather than re-routing
   live reads through user-JWT Postgres connections. Confirm against how
   `auth_magic_link.py` issues/validates sessions.
3. **Identity-space parity.** Confirm the authenticated web user resolves to the
   same `mediator.users.id` that a Discord turn carries as `ctx.user.id`
   (`app/services/turn_context.py:82`). Sprint 2 relies on these being the same
   id; if they diverge, that mapping must be defined here.

## Constraints

- **Security-sensitive**: this app holds intimate relationship data; a wrong
  ownership check = cross-user data exposure. No conversation read/mutate path may
  remain unguarded.
- Must not break the existing live call flow (create → prep → ready → WS start →
  end → debrief → review).
- Preserve a working local/dev path (test user) behind an explicit, clearly-named
  switch — do not silently leave the test-user shim wired into production reads.
- RLS policies on `mediator.conversations` / `conversation_items`
  (`migrations/0042:331-351`) must remain consistent with whatever enforcement
  model is chosen.

## Done criteria

- A web session authenticated as user X can list, open, start, end, and review
  only conversations owned by X (or where X is `partner_user_id`); attempts on
  another user's conversation are rejected (403/404) on every endpoint **and** the
  WebSocket.
- `GET /api/live/sessions` returns exactly the authenticated user's conversations,
  correctly filtered/ordered, with the fields above.
- A conversation row inserted with `user_id = X` (simulating Sprint 2's tool) is
  discoverable via the list endpoint and startable by a session authenticated as
  X — proving the Sprint 2 hand-off works.
- Tests pass: ownership-rejection per endpoint + WS, list scoping, create→discover
  →start round trip.

## Touchpoints

- `app/routers/live_voice.py` — `_resolve_test_user_id:134`; endpoints at `:322`
  (`POST /sessions`), `:477` (`/card`), `:786` (`/end`), `:981` (`/review/save`),
  `:580`/`:643` (retries), `:1493` (`get_session`), `:1535`/`:1584` (WS auth/start)
- `app/routers/auth_magic_link.py` — session issuance / JWT validation
- `app/db.py` — pool / schema access
- `migrations/0042` (RLS policies) — and a new migration only if enforcement model
  requires it
- `web/live-voice/src/App.tsx`, `src/api.ts`, `src/components/SessionCard.tsx` —
  on-entry discovery + list rendering

## Hand-off artifact for Sprint 2

A short written contract (in this brief's plan output or a doc) stating: (a) how a
conversation owner is set and checked, (b) the `GET /api/live/sessions` shape, and
(c) confirmation that `ctx.user.id` (Discord) == the web auth user id. Sprint 2's
`create_conversation_plan` writes `user_id = ctx.user.id` against this contract.
