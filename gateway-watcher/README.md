# gateway-watcher

External health watcher for fleet-critical infra. Runs on **rpi5-230**
(Portainer endpoint 13) — separate physical Pi 5 host, deliberately
NOT on TrueNAS.

## Why this exists

On 2026-05-09 the `sr-agentgateway` stack on TrueNAS went down during
a deploy. `redeployStackGit` returned 500, the orchestration step had
already stopped the old container, and the new one failed to start.
Self-recovery was structurally impossible because every recovery path
(MCP Portainer, MCP Bitwarden, MCP TrueNAS) federates through the
gateway it was trying to recover. Operator restored manually via the
Portainer UI.

The fix is what this watcher does: observe fleet-critical endpoints
from OUTSIDE the failure domain they protect, and fire a
Portainer GitOps recovery webhook (a standalone-credentialed HTTP
call that does NOT depend on the gateway being up) when an endpoint
stays down past a threshold.

See also:
- `~/.claude/rules/agentgateway-deploy-safety.md` — the global rule
  that mandates an external watcher for stack 1183.
- `D:\github-local\.config\agentgateway-recovery.env` — local stash
  of the same webhook UUIDs configured here.

## What it watches (V1)

| Endpoint | Why fleet-critical | Recovery action |
|---|---|---|
| `sr-agentgateway` (8400) | Federated MCP — every agent's tool access | Webhook → stack 1183 redeploy |
| `sr-restate` admin (9070) | Durable execution layer for delegate flows | Webhook → stack 1197 redeploy |
| `sr-nats` monitoring (8222) | Event bus for chat substrate + audit | Webhook → stack 1193 redeploy |
| `litellm` (4000) | LLM gateway — all agents block on it | Webhook → stack 1200 redeploy |

`sr-rag` (stack 1147) is NOT watched in V1 — it has no Portainer
GitOps webhook configured, and degradation there is non-fatal (RAG
queries fail soft).

## Failure model

Connection-level failure (refused / timeout / DNS) = service is dead.
HTTP status code is informational only — a 405 or 500 still means the
service is alive enough to respond. We only fire recovery on genuine
unreachability.

## Tunables (env vars)

| Var | Default | Purpose |
|---|---|---|
| `WATCHER_POLL_INTERVAL_SEC` | 60 | Tick interval |
| `WATCHER_FAILURE_THRESHOLD` | 5 | Consecutive failed ticks before fire |
| `WATCHER_PROBE_TIMEOUT_SEC` | 10 | Per-probe HTTP timeout |
| `WATCHER_WEBHOOK_TIMEOUT_SEC` | 30 | Recovery webhook timeout |
| `WATCHER_COOLDOWN_SEC` | 900 | Post-fire cooldown (15min) before re-firing same endpoint |
| `WATCHER_DRY_RUN` | false | Log fires without POSTing — useful for first soak |
| `LOG_LEVEL` | INFO | Python logging level |

Defaults: 5 consecutive ticks at 60s interval = **5 min downtime
threshold** before recovery fires. Cooldown prevents firing more than
once per 15min per endpoint (a flapping service shouldn't get hammered).

## Resource footprint

A curl-equivalent per minute. Microseconds of CPU, single-digit MB
of RAM, ~200 bytes/min outbound LAN. Runs comfortably on a Pi 5 at
<0.1% CPU + <50MB RAM. Compose enforces explicit caps (64MB / 0.1 CPU)
as a defense against future bugs.

## State

Persists per-endpoint state at
`/var/lib/gateway-watcher/state.json` (named volume
`gateway-watcher-state`). Survives container restart so a flapping
service doesn't get re-armed on every restart. Format:

```json
{
  "sr-agentgateway": {
    "consecutive_failures": 0,
    "last_fire_at": 1715283800.0,
    "last_check_at": 1715283860.5,
    "last_check_ok": true
  }
}
```

## Stack creation

The watcher is deployed as a Portainer GitOps stack on rpi5-230
(endpoint 13), pointing at `whasamatau-homelab/sr-monitoring` repo
with `composeFile: gateway-watcher/docker-compose.yaml` and
`supportRelativePath: true` so the `./watcher.py` bind-mount resolves.

No env vars need to be set on the Portainer stack itself — all config
ships in the compose file.

## First-run procedure

1. Merge this PR into `whasamatau-homelab/sr-monitoring` main.
2. Create the Portainer stack on rpi5-230 (endpoint 13) per "Stack
   creation" above.
3. **Soak in dry-run mode for 24h**: set `WATCHER_DRY_RUN=true` in
   the compose, redeploy. Check logs for false-positive failures
   (especially during planned ops on TrueNAS).
4. After 24h clean, flip `WATCHER_DRY_RUN=false` and redeploy. Live.

## Verification

- After deploy, `docker logs gateway-watcher` should show one
  `endpoint: name=...` line per configured endpoint at startup, then
  silent steady-state (only logs on warnings/recoveries/fires).
- `state.json` content should refresh every 60s — that's also the
  watcher's own healthcheck.
- Synthetic outage test (NOT for production):
  `docker stop sr-agentgateway` on TrueNAS via Portainer, wait
  5+ min, verify the watcher fires the recovery webhook.
  Then `docker start` it back. Reset `state.json` if needed.

## Limitations

- Single-replica. If rpi5-230 itself goes down, no watching happens.
  Acceptable for V1 (rpi5-230 is a single-purpose Pi with very low
  failure rate). V2 could add a peer on rpi5-231 for HA.
- No alerting beyond container logs. V2 candidate: optional Telegram
  notification on fire via sr-telegram-gw.
- The fire-and-forget recovery model assumes the webhook redeploy
  itself succeeds. It doesn't verify post-fire that the endpoint
  came back. V2 candidate: post-fire follow-up probe with
  escalation if recovery didn't take.
