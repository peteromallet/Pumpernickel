# Live Voice Agent — Meta Checklist & Live Plan

**Status:** in progress. Worktree: `/Users/peteromalley/Documents/.megaplan-worktrees/veas-live-voice-agent` on branch `live-voice-agent` (branched from `main`).
**Source briefing:** `~/Downloads/live-voice-agent-briefing.md`.
**Source design doc:** `docs/live-conversation-mode.md` (deeply considered; the briefing scope is tighter — reconciliation below).
**Driver:** Megaplan `chain` over the all-Claude profile, `--depth high` for plan/revise/loop_plan.

## Operating principles (restated every update — non-negotiable)

1. **Never accept blockers** — work around, replace, or rebuild until resolved.
2. **No questions, no approval gates** — every call is mine.
3. **Robustness over speed**, but keep momentum.
4. **Reuse, don't reinvent** — same personas, same DB, same audit primitives.
5. **Validate continuously** — critique at each phase, test in browser, smoke in deploy.
6. **Isolate workspace** — worktree on a fresh branch off `main`.
7. **Done means done** — every item verified end-to-end, deployed live, conversations land in DB.

## Plan critique outcome (3 subagents)

The briefing prescribes "OpenAI voice + Discord OAuth + Railway deploy + reuse personas/DB". The detailed doc `docs/live-conversation-mode.md` prescribes a pipeline (Deepgram + Haiku + ElevenLabs) with first-class consent, coverage criteria, soft focus line, controls footer, prep/live/post-synthesis phases, and a non-skippable review screen. Three critique agents reviewed from UX, reliability/ops, and data/privacy. Consolidated decisions below.

### Reconciliation R1 — Voice stack
- **STT: OpenAI `gpt-4o-mini-transcribe` streaming.** Honors briefing's "voice layer: OpenAI"; existing key already in `.env`.
- **LLM brain: Claude Haiku 4.5 + Opus 4.7.** The `emit_live_turn` structured-output contract is load-bearing (routing/coverage/notes atomic per turn). OpenAI Realtime end-to-end would break this — explicitly out per the doc and per UX critique #S2.
- **TTS: ElevenLabs Flash for personified bots (Tante Rosi).** Persona consistency is a load-bearing product value (UX critique #S1). For non-personified bots (Coach, Hector) where voice palette is less critical, OpenAI TTS is the v1.1 fallback if budget pressure surfaces.
- **Diarization for v1:** solo sessions only. Dyadic / multi-speaker requires Deepgram or equivalent → v1.1.

### Reconciliation R2 — Scope
- Doc's staged rollout wins. v1 ships Phase 1 + Phase 2 (transcript-only live mode with consent + review screen) + Phase 3 (bot turns) before VAD/barge-in. Briefing's "React app fully wired in one shot" is mapped to the same end state via the staged sprints below.

### Reconciliation R3 — Personas + auth
- Persona picker scoped to `user.bot_bindings` (the bot the user is bound to via the existing multi-agent architecture), NOT the global `BOT_SPECS` registry. UX critique #S3.
- Discord OAuth authenticates the primary user against `users.discord_id`. Refuse login if no matching bound user (no self-signup in v1). Partner participates via in-session consent without their own login. Web user_id == Veas user_id via Discord ID join.

### Reconciliation R4 — Data model integrity
- Browser **never** sees the service-role key. WSS connects to a backend orchestrator that holds service-role; browser holds a Discord-OAuth-derived short-lived JWT only.
- Every new live-mode table gets `ENABLE + FORCE ROW LEVEL SECURITY + REVOKE FROM anon + deny_anon policy + owner-scoped policy`. No exceptions.
- `partner_label text` added alongside `partner_user_id uuid NULL` (CHECK at most one set) — partners often won't have accounts. Privacy critique #L2.
- `consent_events` and `speaker_map` become their own tables (`conversation_consent_events`, `conversation_speakers`) — append-only audit-grade, not JSONB. Privacy critiques #S13, #S14.
- Every `emit_live_turn` call writes to existing `tool_calls` + `tool_calls_audit` (migration 0039). Reuse, don't fork. Privacy critique #S15 + reliability #M9.
- Synthesis writes go through existing write tools — observations/distillations/themes/watch_items — wrapped in `pg_advisory_xact_lock(user_id, topic_id)` to prevent multi-session races with Discord text turns. Reliability #M11.
- Audio retention default: **transcript-only**; raw PCM discarded after STT finalization. Privacy critique #L4.

### SLOs (reliability critique)
- Ear-to-ear latency: **p50 ≤ 1.2s, p95 ≤ 2.0s, p99 ≤ 3.5s**.
- Per-session budget: **$2 soft / $4 hard** cap. Per-user: 60 min/day, $10/day.
- Crash-free session rate: **≥99%**.
- Concurrent sessions v1: ≤25 (single-replica ceiling on Railway).

## Sprint breakdown

### Sprint 0 — Reconciliation, scaffolding, migrations (1 week)
**DoD:**
- Worktree created ✓
- Reconciliations R1–R4 documented in this file ✓
- Migration `0042_live_conversations.sql`: `conversations` table + `conversation_items` + `transcript_turns` + `conversation_notes` + `item_visits` + `conversation_consent_events` + `conversation_speakers`. All with `ENABLE + FORCE + REVOKE FROM anon + deny_anon + owner-scoped policies`. CHECK constraints (`partner_user_id XOR partner_label`).
- Migration `0042_live_conversations.down.sql` reverses cleanly.
- New backend module `app/services/live/__init__.py` + skeleton for `orchestrator.py`, `prep.py`, `turn_loop.py`, `synthesis.py`. No behavior yet — imports must work.
- React project scaffolded under `web/live-voice/` (Vite + TypeScript + Tailwind). Discord OAuth stub. Talks to backend over WSS at `/ws/live/:session_id`.
- Frontend reads JWT minted by `/auth/discord/callback` exchanging the Discord OAuth code; backend holds Discord refresh tokens encrypted using existing `crypto.py`.
- `/healthz` endpoint asserts: DB reachable, `mediator.conversations` exists with FORCE RLS, OpenAI key present, ElevenLabs key present.
- Railway service skeleton (Procfile + `railway.toml`) — not deployed yet.
- Smoke test: `pytest tests/test_live_migrations.py` confirms migrations apply, policies in place, anon role rejected on every new table.

**Megaplan idea text:**
> Sprint 0 of the live voice agent: scaffold the backend service module under `app/services/live/`, create migration 0042 for live-conversation tables (conversations, conversation_items, transcript_turns, conversation_notes, item_visits, conversation_consent_events, conversation_speakers) with FORCE RLS + deny-anon + owner-scoped policies + the `partner_user_id XOR partner_label` CHECK, scaffold the React app under `web/live-voice/` (Vite+TS+Tailwind) with a working Discord OAuth callback that mints a short-lived JWT, add `/healthz` asserting DB + migrations + OpenAI key + ElevenLabs key, and add Railway service config (Procfile + railway.toml — not deployed yet). Persona picker queries `bot_bindings` not the global registry. Write tests for migrations + RLS policies. Do NOT add any audio handling, voice, prep, or live-turn logic yet.

### Sprint 1 — Prep + session card (2 weeks)
**DoD:**
- `app/services/live/prep.py`: Opus reads user's `bot_bindings`, longitudinal state, recent distillations, existing themes; produces schema-validated `agenda` JSON (function-calling, schema validated — not prose-then-parse).
- Persisted to `conversations` + `conversation_items` (current_item_id set to the first `must` item).
- React: `/start` page renders the session card from `prep_summary` + items grouped by `theme_id` (humanized — "Where you both are on the timeline", not raw IDs). User can edit focus areas and "Anything to add or avoid?" before pressing Start.
- Streamed phase descriptors over WSS: *Catching up on where you are…*, *Thinking about what to focus on…*, *Getting ready for our chat…*
- No mic, no audio yet. The session card is independently useful (the user can read it before a real human conversation, per doc's stage 1).
- Test: end-to-end prep against a fixture user with mocked Opus call; asserts agenda items pass schema validation, `theme_id`s resolve, all `next_item_ids[]` are present.

**Megaplan idea:**
> Sprint 1: implement Phase 1 of live voice mode end-to-end without audio. Add an Opus-driven prep step that produces a schema-validated agenda for a chosen bot (limited to the user's bot_bindings), persists it as conversations + conversation_items, and renders a session card in the React app via streaming WSS phase descriptors. User can edit focus areas before pressing Start. No mic, no live turn loop, no synthesis. Include fixture-based tests asserting agenda schema validation and theme_id resolution.

### Sprint 2 — Transcript-only live mode + consent (2 weeks)
**DoD:**
- Consent flow: pre-mic "Who is here?" screen → if partner selected, both-voices consent OR shared-screen tap → persists `conversation_consent_events` rows atomically before mic opens. Refuses to accept audio frames without consent.
- React captures mic via Web Audio API → streams PCM to backend WSS.
- Backend streams to OpenAI `gpt-4o-mini-transcribe` for partial + final transcripts. Final transcripts persisted as `transcript_turns` (speaker_label='speaker_0', speaker_role='primary' for solo v1).
- "Advance" button manually moves `conversations.current_item_id` to next item — no Haiku yet.
- Always-visible **Stop recording for everyone** control writes a withdraw event + closes the mic.
- Audio buffer never written to disk (assert via test — `tests/test_no_audio_persistence.py`).
- Bot turn is silent (no TTS) — Phase 3 not in scope yet.
- Browser smoke: open `/live/:session_id`, consent, speak, transcript shows up, advance, end & save → conversation row marked `ended` (no synthesis yet).

**Megaplan idea:**
> Sprint 2: implement Phase 2 (transcript-only live mode) with first-class consent. Pre-mic consent gate persists conversation_consent_events atomically before mic opens. React captures mic via Web Audio API and streams PCM to a WSS endpoint that pipes to OpenAI gpt-4o-mini-transcribe. Final transcripts persist as transcript_turns. Manual Advance button moves the current_item_id. A Stop-recording-for-everyone control writes a withdraw event and closes the mic. Audio frames never persisted. No bot speech, no Haiku yet. Include a test asserting no audio buffer survives the orchestrator request scope.

### Sprint 3 — Bot turns via Haiku `emit_live_turn` + ElevenLabs TTS + review screen (2 weeks)
**DoD:**
- Backend: per-turn Haiku call with prompt-cached agenda + last 6-10 transcript turns + session_fields + progress table. Emits one structured `emit_live_turn` output (utterance + route + coverage + new_items + notes + session_fields_patch).
- Schema-validated. Atomically applied: `conversation_items.status` updates, `current_item_id` advances, `conversation_notes` rows added, `session_fields` JSONB patched.
- ElevenLabs Flash TTS streams audio back to the client over WSS.
- Each `emit_live_turn` call also writes to `tool_calls` + `tool_calls_audit` (migration 0039) — reuse, don't fork.
- Per-turn spend recorded via `app/services/spend.py`; budget guard enforces $2 soft / $4 hard per session.
- React: soft focus line ("We're talking about timing right now") rendered from current item's title. `[Show structure]` toggle reveals item list grouped by theme.
- Controls footer: **Pause, Repeat, Back up, Slow down, Skip this, End & save, End without saving notes**. Back up rewinds one `item_visit` and reverts coverage written in that turn.
- Crisis classifier on each user transcript turn (reuse `crisis_solo.py` + `text_safety.py`). On signal: override Rosi response with scripted grounding + show resource panel.
- Non-skippable review screen on End & save: shows four sections (*What Rosi heard*, *What you decided*, *Still open*, *What Rosi should remember*). Each item editable + deletable. **Save** writes through existing write tools (observations/distillations/themes/watch_items/pregnancy fields) wrapped in `pg_advisory_xact_lock(user_id, topic_id)`. **Discard** keeps transcript + conversation row only.
- Replay tool: `replay_turn(turn_id)` admin endpoint re-runs Haiku with original inputs.
- E2E test: scripted fixture conversation → asserts transcript turns, coverage progression, review screen contents, post-save memory writes.

**Megaplan idea:**
> Sprint 3: wire bot turns through Claude Haiku 4.5 emitting a single schema-validated emit_live_turn structured output per turn, with prompt-cached agenda and atomic apply of route+coverage+new_items+notes+session_fields_patch. Stream TTS audio back over ElevenLabs Flash. Implement the controls footer (Pause/Repeat/Back up/Slow down/Skip/End & save/End without saving). Build the non-skippable post-session review screen with four sections; on Save, write through existing observation/distillation/theme write tools wrapped in pg_advisory_xact_lock. Add a per-turn crisis classifier reusing crisis_solo + text_safety. Each emit_live_turn writes to tool_calls + tool_calls_audit. Enforce $2/$4 per-session budget caps. Include an end-to-end fixture test of a scripted conversation.

### Sprint 4 — Autonomous turn-taking: VAD + barge-in + latency polish (1 week)
**DoD:**
- Client VAD (Silero or energy-threshold) emits `turn_end` after ~600ms silence.
- 10s silence fallback triggers a bot turn.
- Barge-in: client cancels playback + emits `barge_in`; orchestrator cancels in-flight Haiku + TTS; marks `transcript_turns.bot_was_barged=true`.
- Per-stage spans (`asr_finalize`, `orchestrator+db`, `llm_ttft`, `tts_first_byte`) written per turn to `live_session_latency` table.
- p50/p95/p99 measured against SLOs. Pre-warm Haiku + TTS at WS handshake with tiny throwaway call.
- Failure-mode matrix wired (from reliability critique): ASR timeout → "trouble hearing you, try typing" textbox; Haiku timeout → "give me one more second" filler; TTS failure → render bot turn as on-screen text with "(voice unavailable)" tag; WS drop → reconnect within 2s.
- Synthetic-client load test harness replays canned 30s / 5min / 30min PCM fixtures; asserts SLOs hold.

**Megaplan idea:**
> Sprint 4: add client-side VAD (Silero or energy threshold), barge-in (cancel in-flight Haiku + TTS), and a 10s silence fallback. Persist per-stage latency spans to a live_session_latency table. Implement the failure-mode matrix: ASR timeout, Haiku timeout, TTS failure, WS drop — each has a defined UX state, not a spinner. Build a synthetic-client load harness that replays canned PCM fixtures and asserts p50 ≤ 1.2s, p95 ≤ 2.0s, p99 ≤ 3.5s ear-to-ear.

### Sprint 5 — Hardening, Railway deploy, smoke (1 week)
**DoD:**
- Railway service deployed: pinned to `us-east-1`, `min_replicas=1` `max_replicas=1`, 2 vCPU / 4GB.
- Pre-deploy migration job. Deploy fails if migration fails.
- All secrets (OpenAI, ElevenLabs, Discord client secret, Supabase service-role) in Railway env.
- CORS allowlist explicit (web origin only). Rate limit `/ws/connect` 10/min/IP.
- Logs shipped to existing sink with `conversation_id` structured field.
- Alarms: p95 latency > 2s for 5min, daily $ > 80% cap, 5xx > 1% for 5min, WS disconnect rate > 5%.
- Post-deploy smoke: synthetic 30s session → asserts transcript + notes + row counts + $cost ≤ $0.05.
- Chrome-extension verification per briefing checklist: load extension, trigger a synthetic conversation, assert flow works end-to-end.
- Rollback plan documented (Railway one-click revert + `0042_live_conversations.down.sql`).

**Megaplan idea:**
> Sprint 5: deploy to Railway as a new service (us-east-1, single replica, 2vCPU/4GB). Wire migration as a pre-deploy job; deploy fails if migration fails. CORS + WS rate limits + log forwarding with conversation_id field + alarms on p95 latency, daily spend, 5xx rate, WS disconnect rate. Post-deploy smoke runs a 30s synthetic session and verifies transcript + notes + conversation row land in production DB at cost ≤ $0.05. Add Chrome-extension verification per briefing. Document Railway one-click rollback and migration .down.sql path.

## Live status

- [x] Worktree created
- [x] Source briefing + design doc read
- [x] 3 critique subagents spawned and returned
- [x] OpenAI key + personas + DB schema confirmed
- [x] Plan revised + reconciliation R1–R4 chosen
- [x] Sprint breakdown drafted (Sprint 0 + 5)
- [ ] Sprint 0 — Scaffolding + migrations (in progress)
- [ ] Sprint 1 — Prep + session card
- [ ] Sprint 2 — Transcript-only live + consent
- [ ] Sprint 3 — Haiku bot turns + TTS + review screen
- [ ] Sprint 4 — VAD + barge-in + latency polish
- [ ] Sprint 5 — Railway deploy + smoke

Updated after each sprint chunk. Principles restated at every update.
