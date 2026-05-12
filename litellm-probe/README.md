# litellm-probe

Silent-fallback detector for LiteLLM virtual aliases. Runs on **rpi5-230**
(Portainer endpoint 13), separate from TrueNAS where LiteLLM itself
runs.

## Why this exists

Between 2026-05-08 and 2026-05-11, every Rev turn in production was
silently served by `qwen3-coder-next` instead of the declared primary
`kimi-k2:1t`. LiteLLM's fallback chain hid the upstream instability
from operators. Wave 1's persona-discipline verification (15/15 test
prompts) was done against kimi-k2-1t, so when actual upstream had
drifted, our quality guarantees were partial — and we had no signal
until the 5/11 daily-digest hit a 429 cascade and the watchdog fired.

This probe catches that class of failure: a primary upstream that's
intermittently broken, where LiteLLM's fallback transparently serves
the request but the served model isn't the one we tested against.

## What it watches

The 5 production virtual aliases:
- `rev-orchestrator`
- `ops-automation`
- `research-worker`
- `trading-agents-analyst`
- `discord-bot-text`

## Detection signal

LiteLLM exposes `x-litellm-attempted-fallbacks` as a response header on
every chat completion. Zero means the primary deployment served the
request; non-zero means LiteLLM walked the fallback chain. This is the
single authoritative signal — no substring matching, no
deployment-specific heuristics, no need to track expected upstream
identifiers per virtual.

The probe also records `x-litellm-model-id` (which becomes the
fallback's name on fallback firings — e.g. `crucible-fast` when
rev-orchestrator falls) so the alert message tells the operator
exactly what's serving instead of the primary.

## Alerting

On `UNEXPECTED_THRESHOLD` consecutive non-primary servings, fires a
Telegram message via sr-telegram-gw with:
- Which virtual drifted
- Expected primary signature
- Actually-served-by identifier
- Sustained duration
- Pointer to investigate

Cooldown (`COOLDOWN_SEC`, default 1h) prevents repeat alerts for the
same drift. Once primary recovers, the consecutive counter resets and
the next drift surfaces a fresh alert after the threshold.

## Tunables (env)

| Var | Default | Purpose |
|---|---|---|
| `PROBE_INTERVAL_SEC` | 300 | Cycle interval (5 min) |
| `UNEXPECTED_THRESHOLD` | 6 | Consecutive non-primary servings before alert (≈30 min) |
| `PROBE_TIMEOUT_SEC` | 30 | Per-probe HTTP timeout |
| `COOLDOWN_SEC` | 3600 | Post-alert cooldown per virtual |
| `LITELLM_BASE` | `http://192.168.1.235:4000` | LiteLLM endpoint |
| `LITELLM_KEY` | `sk-local-ai-ecosystem` | LiteLLM master key |
| `TELEGRAM_BASE` | `http://192.168.1.235:18792` | sr-telegram-gw endpoint |
| `OPENCLAW_HOOKS_TOKEN` | (required for alerts) | Telegram auth token |
| `TELEGRAM_MUTE` | false | Log alerts without sending (first-run soak) |
| `LOG_LEVEL` | INFO | Python logging level |

`OPENCLAW_HOOKS_TOKEN` is a secret — set in the Portainer stack env,
NOT in the compose file. Same token sr-claw-rev uses for daily-digest
delivery; reuse for symmetry.

## Resource footprint

5 small POSTs every 5 minutes. <0.05% CPU, single-digit MB RAM,
~1 KB/min outbound LAN. Compose enforces 64MB / 0.1 CPU caps.

## State

`/var/lib/litellm-probe/state.json` (named volume
`litellm-probe-state`). Per-virtual:
- `consecutive_unexpected` — running counter
- `last_seen_primary_at` — last time primary was confirmed
- `last_alert_at` — for cooldown
- `last_check_at`, `last_served_by` — most recent probe result

Survives container restart so a flapping primary doesn't reset the
streak on every restart.

## Updating the virtual list

The `VIRTUALS` list in `probe.py` is the set of aliases the probe
watches. When `whasamatau-homelab/litellm/config.yaml` adds or retires
a virtual (e.g. a new caller-tuned alias for some new fleet), update
this list in the same PR (or shortly after — a missing entry just
means no coverage, not a false alert).

Crucially, **the probe does NOT need to know the expected upstream**
for each virtual. LiteLLM's `x-litellm-attempted-fallbacks` header
makes that determination authoritatively for us.

## First-run procedure

1. Merge to `whasamatau-homelab/sr-monitoring` main.
2. Create the Portainer stack on rpi5-230 (endpoint 13). Set
   `OPENCLAW_HOOKS_TOKEN` in stack env.
3. (Optional) Set `TELEGRAM_MUTE=true` for 24h soak to confirm no
   false-positives.
4. Flip `TELEGRAM_MUTE=false` after soak.

## What this explicitly does NOT do

- Doesn't fire recovery webhooks. Drift here is upstream-provider or
  config-policy territory — operator decides whether to repoint
  primary, wait it out, or restructure the fallback chain. Mechanical
  recovery would be wrong.
- Doesn't probe non-virtual `ollama-*` aliases. Bare aliases are
  called explicitly by their callers; if they fail the caller sees it
  immediately. Silent-fallback risk is virtual-specific (the wrapped
  semantics is what makes fallback transparent).
- Doesn't measure response quality, latency, or tool-call accuracy.
  Single-shot "reply OK" probe only checks the routing path. Quality
  evaluation is a separate concern.

## Limitations

- Single-replica on rpi5-230. If the Pi goes down, no probing happens.
  Acceptable; the Pi failure rate is very low and the gateway-watcher
  on the same Pi covers infrastructure liveness separately.
- The probe itself consumes a tiny amount of upstream quota (5 calls
  per 5min × Ollama Cloud Pro at 60 RPM = ~0.5% of bucket). If a
  caller is already at quota saturation, the probe could be the
  straw that triggers a 429 — but in that case the bucket is the
  bigger problem and the probe surfacing it earlier is useful.
- Couples to LiteLLM's response header contract
  (`x-litellm-attempted-fallbacks`, `x-litellm-model-id`). If a
  future LiteLLM upgrade removes or renames these headers, the
  probe will silently treat every call as "primary served" (the
  header default is 0). Watch for: LiteLLM upstream release notes
  mentioning header changes, and the probe's own logs going
  suspiciously quiet across all 5 virtuals at once.
