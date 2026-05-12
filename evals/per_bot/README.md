# Per-Bot Eval Scenarios

Per-bot evaluation scenarios live under `evals/per_bot/<bot_id>/`.  
No scenarios are filled out yet — future sprints will add them.

Each bot's directory mirrors the structure under `evals/scenarios/` but is
isolated so that eval runs can target a single bot's behaviour (read scopes,
write scopes, participants_shape, cross-topic policy, etc.) without
accidentally exercising another bot's configuration.