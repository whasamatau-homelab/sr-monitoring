#!/usr/bin/env python3
"""
gateway-watcher — external health watcher for fleet-critical infra.

Polls a small set of HTTP endpoints from outside the failure domain
they protect. When an endpoint stays unreachable for >FAILURE_THRESHOLD
consecutive ticks, fires a targeted Portainer GitOps webhook to
recover the corresponding stack.

Designed to run on rpi5-230 (Portainer endpoint 13) — separate physical
host from TrueNAS (where most watched services live). The whole point
is to NOT ride the failure domain you're watching, so do not run this
on TrueNAS.

Why this exists: 2026-05-09 outage of sr-agentgateway. redeployStackGit
500'd, container ended up stopped, every MCP recovery path rode the
dead gateway, operator had to restore manually via Portainer UI. An
external watcher fixes that class of failure mode by observing from
outside and firing the standalone-credentialed webhook.

Failure model: "did the HTTP request complete at all?" Status code is
informational only — a 405 or 500 still means the service is alive
enough to respond. Connection-refused / DNS-fail / timeout = dead.

State: a small JSON file at /var/lib/gateway-watcher/state.json
tracking consecutive_failures + last_fire_at per endpoint. Survives
container restart so we don't double-fire on a flapping service.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Configurable via env. Defaults are conservative for a 60s tick.
POLL_INTERVAL_SEC = int(os.environ.get("WATCHER_POLL_INTERVAL_SEC", "60"))
FAILURE_THRESHOLD = int(os.environ.get("WATCHER_FAILURE_THRESHOLD", "5"))  # 5 ticks * 60s = 5min
PROBE_TIMEOUT_SEC = int(os.environ.get("WATCHER_PROBE_TIMEOUT_SEC", "10"))
WEBHOOK_TIMEOUT_SEC = int(os.environ.get("WATCHER_WEBHOOK_TIMEOUT_SEC", "30"))
COOLDOWN_AFTER_FIRE_SEC = int(os.environ.get("WATCHER_COOLDOWN_SEC", "900"))  # 15min — don't spam
PORTAINER_BASE = os.environ.get("PORTAINER_BASE", "http://192.168.1.235:31014")
STATE_PATH = Path(os.environ.get("WATCHER_STATE_PATH", "/var/lib/gateway-watcher/state.json"))
DRY_RUN = os.environ.get("WATCHER_DRY_RUN", "").lower() in ("true", "1", "yes")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
)
log = logging.getLogger("gateway-watcher")


@dataclass
class Endpoint:
    name: str
    url: str  # full URL to probe
    webhook_uuid: str  # Portainer GitOps webhook UUID for recovery
    method: str = "GET"  # GET / HEAD; HEAD where supported


@dataclass
class State:
    consecutive_failures: int = 0
    last_fire_at: float = 0.0  # unix timestamp
    last_check_at: float = 0.0
    last_check_ok: bool = True


def load_endpoints() -> list[Endpoint]:
    """Read the endpoints config from env. Format:
    WATCHER_ENDPOINTS=name1=url1::uuid1,name2=url2::uuid2
    URL and UUID separated by ::, endpoints separated by commas. The
    `::` choice avoids conflict with URL embedded `=` (auth tokens etc).
    """
    raw = os.environ.get("WATCHER_ENDPOINTS", "")
    endpoints: list[Endpoint] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            name, rest = entry.split("=", 1)
            url, uuid = rest.split("::", 1)
            endpoints.append(Endpoint(name=name.strip(), url=url.strip(), webhook_uuid=uuid.strip()))
        except ValueError:
            log.error("malformed WATCHER_ENDPOINTS entry: %s", entry)
    if not endpoints:
        log.error("no endpoints configured — set WATCHER_ENDPOINTS env var")
        sys.exit(2)
    return endpoints


def load_state() -> dict[str, State]:
    if not STATE_PATH.exists():
        return {}
    try:
        raw = json.loads(STATE_PATH.read_text())
        return {name: State(**s) for name, s in raw.items()}
    except (json.JSONDecodeError, TypeError) as exc:
        log.warning("state file corrupt; starting fresh: %s", exc)
        return {}


def save_state(state: dict[str, State]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    serialized = {name: s.__dict__ for name, s in state.items()}
    STATE_PATH.write_text(json.dumps(serialized, indent=2))


def probe(endpoint: Endpoint) -> tuple[bool, str]:
    """Returns (alive, reason). `alive=True` means the endpoint responded
    at all (any HTTP status). `alive=False` means connection-level
    failure (refused, timeout, dns) — the service is unreachable."""
    req = urllib.request.Request(endpoint.url, method=endpoint.method)
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_SEC) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        # Got a response with a non-2xx code — service is alive.
        return True, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        # Connection-level failure.
        return False, f"URLError: {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def fire_webhook(endpoint: Endpoint) -> tuple[bool, str]:
    url = f"{PORTAINER_BASE}/api/stacks/webhooks/{endpoint.webhook_uuid}"
    if DRY_RUN:
        log.info("DRY_RUN: would POST %s", url)
        return True, "dry-run"
    req = urllib.request.Request(url, method="POST", data=b"")
    try:
        with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT_SEC) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def tick(endpoints: Iterable[Endpoint], state: dict[str, State]) -> None:
    now = time.time()
    for ep in endpoints:
        alive, reason = probe(ep)
        s = state.setdefault(ep.name, State())
        s.last_check_at = now
        s.last_check_ok = alive
        if alive:
            if s.consecutive_failures > 0:
                log.info(
                    'recovered: name=%s previous_failures=%d reason=%s',
                    ep.name, s.consecutive_failures, reason,
                )
            s.consecutive_failures = 0
        else:
            s.consecutive_failures += 1
            log.warning(
                'failed: name=%s consecutive=%d reason=%s url=%s',
                ep.name, s.consecutive_failures, reason, ep.url,
            )
            if s.consecutive_failures >= FAILURE_THRESHOLD:
                if now - s.last_fire_at < COOLDOWN_AFTER_FIRE_SEC:
                    log.info(
                        'threshold reached but in cooldown: name=%s seconds_remaining=%d',
                        ep.name,
                        int(COOLDOWN_AFTER_FIRE_SEC - (now - s.last_fire_at)),
                    )
                else:
                    log.warning(
                        'firing recovery webhook: name=%s threshold=%d',
                        ep.name, FAILURE_THRESHOLD,
                    )
                    ok, hook_reason = fire_webhook(ep)
                    if ok:
                        log.warning(
                            'recovery webhook fired: name=%s response=%s',
                            ep.name, hook_reason,
                        )
                        s.last_fire_at = now
                    else:
                        log.error(
                            'recovery webhook failed: name=%s reason=%s',
                            ep.name, hook_reason,
                        )


def main() -> int:
    endpoints = load_endpoints()
    log.info(
        'starting: endpoints=%d poll=%ds threshold=%d cooldown=%ds dry_run=%s',
        len(endpoints), POLL_INTERVAL_SEC, FAILURE_THRESHOLD,
        COOLDOWN_AFTER_FIRE_SEC, DRY_RUN,
    )
    for ep in endpoints:
        log.info('endpoint: name=%s url=%s', ep.name, ep.url)
    state = load_state()
    while True:
        try:
            tick(endpoints, state)
            save_state(state)
        except Exception as exc:  # noqa: BLE001
            log.error('tick failed: %s', exc)
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    sys.exit(main())
