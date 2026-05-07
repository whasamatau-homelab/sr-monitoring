# sr-monitoring (proposed) — MP-21

Loki + Promtail + Grafana for centralized fleet log aggregation.

## Status

**Scaffolded but not deployed.** Files live here under sr-claw-rev
until the operator decides the deploy trigger from master-plan §2.5.5
is met (5+ instances OR a cross-instance debug incident
fleet-health can't satisfy). Currently 6 sr-claw-* + adjacent
stacks → threshold is borderline-met.

## Files

- `docker-compose.yaml` — three-service stack (loki, promtail,
  grafana). Bind mounts `/mnt/data-pool/apps/sr-monitoring/{loki,
  grafana,promtail}` per existing TrueNAS-app pattern.
- `loki-config.yaml` — single-binary Loki, 7-day retention,
  cardinality limits to defend against label explosion.
- `promtail-config.yaml` — docker_sd against the host socket;
  filters to only sr-* compose projects; extracts JSON `level` for
  scalable filtering without ingesting free-form fields.

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

## Why not SigNoz?

SigNoz is already deployed for traces + metrics. Loki is
complementary: it specifically handles log aggregation with LogQL.
Per master-plan §2.5.5: "centralized logs become valuable at 5+;
required at 10+." SigNoz could absorb the role via its
ClickHouse-backed log store, but Loki is the canonical OSS log
endpoint and avoids overloading the SigNoz collector.

If a future consolidation passes prefer SigNoz log ingestion,
this scaffolding can be retired and the Promtail config rewritten
to push to the SigNoz OTel collector instead.
