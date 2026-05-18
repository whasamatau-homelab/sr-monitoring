# sr-monitoring (proposed) — MP-21

Loki + Promtail + Grafana for centralized fleet log aggregation.

## Status

**Deployed** as Portainer stack 1210. Extended 2026-05-17 with metrics +
traces + OTLP ingest (otel-collector / prometheus / tempo), replacing the
retired SigNoz stack.

## Files

- `docker-compose.yaml` — six-service stack: loki, promtail, grafana,
  otel-collector, prometheus, tempo. Bind mounts
  `/mnt/data-pool/apps/sr-monitoring/{loki,grafana,promtail,prometheus,tempo}`
  per the TrueNAS-app pattern.
- `loki-config.yaml` — single-binary Loki, 7-day retention,
  cardinality limits to defend against label explosion.
- `promtail-config.yaml` — docker_sd against the host socket;
  filters to only sr-* compose projects; extracts JSON `level` for
  scalable filtering without ingesting free-form fields.
- `otel-collector-config.yaml` — OTLP ingest (4317/4318); fans out to
  Loki (logs) / Prometheus (metrics) / Tempo (traces). Static config.
- `prometheus.yml` — metrics store; receives via remote-write.
- `tempo.yaml` — single-binary trace store, 7-day retention.

## Operator deploy steps

```bash
# 1. Create the upstream repo
gh repo create whasamatau-homelab/sr-monitoring --public

# 2. Move the scaffolded files
mv sr-claw-rev/docs/proposed-stacks/sr-monitoring/* whasamatau-homelab/sr-monitoring/
cd whasamatau-homelab/sr-monitoring
git init && git add . && git commit -m "initial sr-monitoring stack"
git push -u origin main

# 3. Provision the TrueNAS datasets (see ~/.claude/rules/docker-volumes.md)
#    Loki UID=10001, Grafana UID=472. Set ownership before stack-create.

# 4. Create the Portainer GitOps stack via API:
#    POST /api/stacks/create/standalone/repository?endpointId=11
#    body: name=sr-monitoring, repositoryURL, repositoryAuthentication=true,
#          repositoryGitCredentialID=17, composeFile=docker-compose.yaml,
#          autoUpdate.webhook=<uuid>, autoUpdate.interval=5m,
#          autoUpdate.forceUpdate=false, autoUpdate.forcePullImage=true,
#          supportRelativePath=true,
#          filesystemPath=/mnt/data-pool/apps/sr-monitoring/repo,
#          env=[{"name":"GRAFANA_ADMIN_PASSWORD","value":"<from-vault>"}]

# 5. Validate
#    curl http://192.168.1.235:3100/ready    # Loki ready
#    curl http://192.168.1.235:3000/api/health  # Grafana up
```

## fleet-health Loki integration (M2)

After the stack lands, extend `sr-claw-sre`'s fleet-health skill to
query Loki for recent error patterns. Master-plan §2.5.5 M2.
Suggested query: `{stack=~"sr-.+", level="error"} | last 1h | count`.

If results > N, fleet-health includes a "recent errors" line in its
Telegram digest. Doesn't replace the per-probe healthcheck — it's a
secondary signal.

## Why upstream OpenTelemetry, not SigNoz

SigNoz was retired 2026-05-17. Its collector is a fork managed remotely
over OpAMP by the SigNoz server; a Watchtower-driven version skew between
the two left the collector unable to apply config, so its OTLP receivers
never came up and fleet telemetry was silently lost.

The replacement is the upstream OpenTelemetry Collector with a **static
config file** — no server pushes config, so that whole skew failure class
cannot recur. It is the vendor-neutral CNCF standard: OTLP in, fan out to
best-of-breed stores (Loki / Prometheus / Tempo), one Grafana pane of glass.
Every image is version/digest-pinned and Renovate-tracked; no Watchtower.
