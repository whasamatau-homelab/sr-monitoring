#!/usr/bin/env python3
"""
litellm-probe — silent-fallback detector for LiteLLM virtual aliases.

Every PROBE_INTERVAL_SEC, sends a tiny prompt to each production virtual
alias through LiteLLM. Reads the `x-litellm-attempted-fallbacks` response
header — LiteLLM increments this when fallback fires. Zero means the
primary upstream served; non-zero means a silent fallback happened.

When a virtual has had non-zero attempted_fallbacks for
UNEXPECTED_THRESHOLD consecutive checks, fires a Telegram alert via
sr-telegram-gw. Cooldown window prevents repeat alerts for the same
drift.

Designed to catch the failure class that bit us 2026-05-08 → 2026-05-11:
rev-orchestrator's primary (kimi-k2:1t) was intermittently failing at
Ollama Cloud, and LiteLLM was silently falling back to qwen3-coder-next
for days. Persona-quality guarantees verified at task #73 against
kimi-k2-1t were therefore partial because actual upstream had drifted.

Runs on rpi5-230 alongside gateway-watcher — same Pi, same external-
observer pattern, but observation-only (no recovery action; that's the
operator's call).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

PROBE_INTERVAL_SEC = int(os.environ.get("PROBE_INTERVAL_SEC", "300"))
UNEXPECTED_THRESHOLD = int(os.environ.get("UNEXPECTED_THRESHOLD", "6"))
PROBE_TIMEOUT_SEC = int(os.environ.get("PROBE_TIMEOUT_SEC", "30"))
COOLDOWN_SEC = int(os.environ.get("COOLDOWN_SEC", "3600"))
LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://192.168.1.235:4000")
LITELLM_KEY = os.environ.get("LITELLM_KEY", "sk-local-ai-ecosystem")
TELEGRAM_BASE = os.environ.get("TELEGRAM_BASE", "http://192.168.1.235:18792")
TELEGRAM_TOKEN = os.environ.get("OPENCLAW_HOOKS_TOKEN", "")
TELEGRAM_MUTE = os.environ.get("TELEGRAM_MUTE", "").lower() in ("true", "1", "yes")
STATE_PATH = Path(os.environ.get("STATE_PATH", "/var/lib/litellm-probe/state.json"))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
)
log = logging.getLogger("litellm-probe")

# The 5 production virtual aliases per
# whasamatau-homelab/litellm/docs/VIRTUAL-MODELS.md.
# A virtual not in this list is invisible to the probe — to add coverage,
# append here.
VIRTUALS: list[str] = [
    "rev-orchestrator",
    "ops-automation",
    "research-worker",
    "trading-agents-analyst",
    "discord-bot-text",
]


@dataclass
class State:
    consecutive_unexpected: int = 0
    last_seen_primary_at: float = 0.0
    last_alert_at: float = 0.0
    last_check_at: float = 0.0
    last_served_by: str = ""
    last_fallback_count: int = 0


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


def probe(virtual: str) -> tuple[bool, int, str, str]:
    """Returns (success, attempted_fallbacks, served_by, reason).
    `attempted_fallbacks` is the value from LiteLLM's
    x-litellm-attempted-fallbacks header (0 = primary served).
    `served_by` is x-litellm-model-id (the final deployment name —
    differs from the requested alias when fallback fires).
    `success=False` means the probe call itself failed (network/timeout/5xx).
    """
    body = json.dumps({
        "model": virtual,
        "messages": [{"role": "user", "content": "reply OK"}],
        "max_tokens": 10,
    }).encode()
    req = urllib.request.Request(
        f"{LITELLM_BASE}/v1/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LITELLM_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_SEC) as resp:
            fallback_count = int(resp.headers.get("x-litellm-attempted-fallbacks", "0") or "0")
            served_by = resp.headers.get("x-litellm-model-id", "") or ""
            # Drain body to release the connection (we don't care about
            # the content, just the headers).
            resp.read()
            return True, fallback_count, served_by, "ok"
    except urllib.error.HTTPError as exc:
        return False, 0, "", f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, 0, "", f"{type(exc).__name__}: {exc}"


def send_telegram(text: str) -> bool:
    if TELEGRAM_MUTE:
        log.info("telegram muted; would have sent: %s", text[:100])
        return True
    if not TELEGRAM_TOKEN:
        log.warning("OPENCLAW_HOOKS_TOKEN unset; cannot send alert")
        return False
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        f"{TELEGRAM_BASE}/telegram/send",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TELEGRAM_TOKEN}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:  # noqa: BLE001
        log.error("telegram send failed: %s", exc)
        return False


def tick(virtuals: Iterable[str], state: dict[str, State]) -> None:
    now = time.time()
    for name in virtuals:
        success, fallback_count, served_by, reason = probe(name)
        s = state.setdefault(name, State())
        s.last_check_at = now

        if not success:
            # Probe failure (network/5xx) is not a drift signal — it's an
            # outright outage. Log loudly but don't count toward
            # consecutive_unexpected (which is specifically the
            # "served-but-by-fallback" signal).
            log.warning(
                'probe failed: virtual=%s reason=%s',
                name, reason,
            )
            continue

        s.last_served_by = served_by
        s.last_fallback_count = fallback_count

        if fallback_count == 0:
            # Primary served.
            if s.consecutive_unexpected > 0:
                log.info(
                    'primary restored: virtual=%s served_by=%s previous_streak=%d',
                    name, served_by, s.consecutive_unexpected,
                )
            s.consecutive_unexpected = 0
            s.last_seen_primary_at = now
            log.info('primary ok: virtual=%s served_by=%s', name, served_by)
        else:
            # Fallback fired.
            s.consecutive_unexpected += 1
            log.warning(
                'fallback fired: virtual=%s served_by=%s fallback_count=%d consecutive=%d',
                name, served_by, fallback_count, s.consecutive_unexpected,
            )
            if s.consecutive_unexpected >= UNEXPECTED_THRESHOLD:
                if now - s.last_alert_at < COOLDOWN_SEC:
                    log.info(
                        'threshold reached but in cooldown: virtual=%s seconds_remaining=%d',
                        name,
                        int(COOLDOWN_SEC - (now - s.last_alert_at)),
                    )
                else:
                    duration_min = int(s.consecutive_unexpected * PROBE_INTERVAL_SEC / 60)
                    text = (
                        f"LiteLLM silent-fallback alert\n"
                        f"\n"
                        f"Virtual: {name}\n"
                        f"Served by: {served_by} (fallback level {fallback_count})\n"
                        f"Sustained for: {duration_min} min "
                        f"({s.consecutive_unexpected} consecutive probes)\n"
                        f"\n"
                        f"The primary upstream is failing and LiteLLM is "
                        f"transparently serving from the fallback chain. "
                        f"Investigate upstream provider; check "
                        f"whasamatau-homelab/litellm/config.yaml + container logs."
                    )
                    if send_telegram(text):
                        s.last_alert_at = now
                        log.warning(
                            'alert sent: virtual=%s consecutive=%d',
                            name, s.consecutive_unexpected,
                        )


def main() -> int:
    log.info(
        'starting: virtuals=%d interval=%ds threshold=%d cooldown=%ds telegram_muted=%s',
        len(VIRTUALS), PROBE_INTERVAL_SEC, UNEXPECTED_THRESHOLD,
        COOLDOWN_SEC, TELEGRAM_MUTE,
    )
    for name in VIRTUALS:
        log.info('virtual: name=%s', name)
    state = load_state()
    while True:
        try:
            tick(VIRTUALS, state)
            save_state(state)
        except Exception as exc:  # noqa: BLE001
            log.error('tick failed: %s', exc)
        time.sleep(PROBE_INTERVAL_SEC)


if __name__ == "__main__":
    sys.exit(main())
