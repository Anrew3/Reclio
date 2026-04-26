"""Hourly background sanity check for every external dependency.

Runs five checks in parallel: DB, Trakt, TMDB, Recombee, LLM. Each
check returns a structured CheckResult; the snapshot is recorded in
a 24-entry rolling buffer (one day at hourly cadence).

Logging strategy — deliberately quiet on the happy path:

  ok        → ok    : silent (DEBUG only)
  ok        → fail  : WARNING with deep-dive diagnostic detail
  ok        → degrade: WARNING with reason
  fail/degrade → ok : INFO "recovered"
  fail      → fail  : DEBUG (don't spam — but admin endpoint can see it)

When a check FAILS, it runs deeper diagnostics specific to that
service (Recombee: full diagnose verdict; LLM: actual generation
test with provider name + key length; etc.) and logs a structured
"diagnostic" object so the operator has everything they need
without having to re-run anything by hand.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Deque, Optional

from sqlalchemy import func, select, text

from app.database import session_scope
from app.models.content import ContentCatalog

logger = logging.getLogger(__name__)


# ---- Data shapes -------------------------------------------------

@dataclass
class CheckResult:
    name: str
    status: str  # 'ok' | 'degraded' | 'failed'
    elapsed_ms: int
    detail: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class HealthSnapshot:
    timestamp: datetime
    overall: str
    elapsed_ms: int
    checks: dict[str, CheckResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "overall": self.overall,
            "elapsed_ms": self.elapsed_ms,
            "checks": {name: asdict(c) for name, c in self.checks.items()},
        }


# ---- Rolling buffer ----------------------------------------------

_HISTORY_SIZE = 24  # 24h at 1/hr cadence
_history: Deque[HealthSnapshot] = deque(maxlen=_HISTORY_SIZE)
_history_lock = asyncio.Lock()
_last_snapshot: Optional[HealthSnapshot] = None


def get_recent_history() -> list[HealthSnapshot]:
    """Snapshot of the rolling buffer for admin display."""
    return list(_history)


def get_last_snapshot() -> Optional[HealthSnapshot]:
    return _last_snapshot


# ---- Individual checks ------------------------------------------

_PROBE_TIMEOUT = 8.0   # generous — we're not in the request path
_LLM_TIMEOUT = 10.0


def _ms_since(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


async def _check_database() -> CheckResult:
    t0 = time.monotonic()
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
        return CheckResult("database", "ok", _ms_since(t0))
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "database", "failed", _ms_since(t0),
            detail={"exception_type": type(exc).__name__},
            error=str(exc)[:200],
        )


async def _check_trakt() -> CheckResult:
    """Trakt: GET /genres/movies (no auth needed). Surface rate-limit
    headers if the API is feeling chatty about its quota."""
    t0 = time.monotonic()
    try:
        from app.services.trakt import get_trakt
        client = get_trakt()
        c = await client._get_client()  # noqa: SLF001
        resp = await asyncio.wait_for(c.get("/genres/movies"), timeout=_PROBE_TIMEOUT)
        detail: dict[str, Any] = {"status_code": resp.status_code}
        # Trakt sends X-Ratelimit headers on every response
        rl_remaining = resp.headers.get("x-ratelimit-remaining")
        rl_limit = resp.headers.get("x-ratelimit-limit")
        if rl_remaining or rl_limit:
            detail["rate_limit"] = {"remaining": rl_remaining, "limit": rl_limit}
        if resp.status_code != 200:
            return CheckResult(
                "trakt", "failed", _ms_since(t0),
                detail=detail,
                error=f"HTTP {resp.status_code}: {resp.text[:160]}",
            )
        return CheckResult("trakt", "ok", _ms_since(t0), detail=detail)
    except asyncio.TimeoutError:
        return CheckResult("trakt", "failed", _ms_since(t0),
                           error=f"timeout after {_PROBE_TIMEOUT}s")
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "trakt", "failed", _ms_since(t0),
            detail={"exception_type": type(exc).__name__},
            error=str(exc)[:200],
        )


async def _check_tmdb() -> CheckResult:
    t0 = time.monotonic()
    try:
        from app.services.tmdb import get_tmdb
        client = get_tmdb()
        if not client._api_key:  # noqa: SLF001
            return CheckResult(
                "tmdb", "failed", _ms_since(t0),
                detail={"hint": "TMDB_API_KEY env var not set"},
                error="TMDB_API_KEY missing",
            )
        c = await client._get_client()  # noqa: SLF001
        resp = await asyncio.wait_for(
            c.get("/configuration", params={"api_key": client._api_key}),  # noqa: SLF001
            timeout=_PROBE_TIMEOUT,
        )
        detail: dict[str, Any] = {"status_code": resp.status_code}
        if resp.status_code == 401:
            return CheckResult(
                "tmdb", "failed", _ms_since(t0), detail=detail,
                error="HTTP 401 — TMDB_API_KEY rejected (check value)",
            )
        if resp.status_code != 200:
            return CheckResult(
                "tmdb", "failed", _ms_since(t0), detail=detail,
                error=f"HTTP {resp.status_code}: {resp.text[:160]}",
            )
        return CheckResult("tmdb", "ok", _ms_since(t0), detail=detail)
    except asyncio.TimeoutError:
        return CheckResult("tmdb", "failed", _ms_since(t0),
                           error=f"timeout after {_PROBE_TIMEOUT}s")
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "tmdb", "failed", _ms_since(t0),
            detail={"exception_type": type(exc).__name__},
            error=str(exc)[:200],
        )


async def _check_recombee() -> CheckResult:
    """Recombee: actually call ListItems + compare counts.

    Surfaces the same `verdict` taxonomy as the /admin/recombee/diagnose
    endpoint (wrong_region / writes_silently_failing / etc.) so the log
    line tells the operator exactly what's broken without further prodding.
    """
    t0 = time.monotonic()
    try:
        from app.services.recombee import get_recombee
        recombee = get_recombee()
        config = recombee.config_dump()

        if not config["sdk_loaded"]:
            return CheckResult(
                "recombee", "failed", _ms_since(t0),
                detail={"config": config, "verdict": "sdk_missing"},
                error="recombee-api-client SDK not installed",
            )
        if not config["token_present"] or not config["database_id"]:
            return CheckResult(
                "recombee", "degraded", _ms_since(t0),
                detail={"config": config, "verdict": "no_credentials"},
                error="RECOMBEE_DATABASE_ID or RECOMBEE_PRIVATE_TOKEN missing",
            )
        if not config["available"]:
            return CheckResult(
                "recombee", "failed", _ms_since(t0),
                detail={"config": config, "verdict": "client_init_failed"},
                error="Recombee client failed to initialize despite credentials present",
            )

        rec_count, rec_sample = await recombee.list_items_count(max_count=3)
        if rec_count is None:
            # Connectivity failure — try a write+read probe for deeper signal
            write_ok, write_err = await recombee.write_test_item()
            return CheckResult(
                "recombee", "failed", _ms_since(t0),
                detail={
                    "config": config,
                    "verdict": "unreachable",
                    "write_probe": {"ok": write_ok, "error": write_err},
                },
                error=("Recombee unreachable (ListItems returned None). "
                       "Most likely wrong DB ID, wrong token type "
                       "(public vs private), or wrong region."),
            )

        # Catalog comparison — detects wrong_region (the most common silent fail)
        async with session_scope() as session:
            catalog_synced = await session.scalar(
                select(func.count()).select_from(ContentCatalog).where(
                    ContentCatalog.recombee_synced.is_(True)
                )
            )
        catalog_synced = catalog_synced or 0

        if catalog_synced > 5 and rec_count == 0:
            return CheckResult(
                "recombee", "failed", _ms_since(t0),
                detail={
                    "config": config,
                    "verdict": "wrong_region",
                    "reclio_marked_synced": catalog_synced,
                    "recombee_returned_count": rec_count,
                    "remediation": (
                        "RECOMBEE_REGION almost certainly wrong. Check the URL "
                        "in the Recombee web UI — it contains the actual region "
                        "(rapi-eu-west.recombee.com etc). Match RECOMBEE_REGION "
                        "to one of: US_WEST / EU_WEST / AP_SE / CA_EAST."
                    ),
                },
                error=(f"Reclio marked {catalog_synced} items synced but "
                       f"Recombee shows 0 — almost always wrong RECOMBEE_REGION"),
            )
        if catalog_synced > 25 and rec_count < catalog_synced // 4:
            return CheckResult(
                "recombee", "degraded", _ms_since(t0),
                detail={
                    "config": config,
                    "verdict": "writes_silently_failing",
                    "reclio_marked_synced": catalog_synced,
                    "recombee_returned_count": rec_count,
                },
                error=(f"Reclio synced {catalog_synced} items but Recombee "
                       f"only shows {rec_count} — many writes silently failed"),
            )

        return CheckResult(
            "recombee", "ok", _ms_since(t0),
            detail={
                "verdict": "ok",
                "recombee_item_count_visible": rec_count,
                "reclio_marked_synced": catalog_synced,
                "sample_ids": rec_sample,
            },
        )
    except asyncio.TimeoutError:
        return CheckResult("recombee", "failed", _ms_since(t0),
                           error=f"timeout after {_PROBE_TIMEOUT}s")
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "recombee", "failed", _ms_since(t0),
            detail={"exception_type": type(exc).__name__},
            error=str(exc)[:200],
        )


async def _check_llm() -> CheckResult:
    """LLM: actually call .generate() with a tiny prompt.

    Most failures here come from invalid keys / regional outages /
    misconfigured Ollama URL — none of which the v1.5.0 /health
    endpoint catches because it only checks the `enabled` flag.
    """
    t0 = time.monotonic()
    try:
        from app.services.llm import get_llm, NullProvider
        llm = get_llm()

        if isinstance(llm.provider, NullProvider) or not llm.enabled:
            # Valid configuration — degraded so it shows up in admin views
            # but not loud enough to wake anyone up.
            return CheckResult(
                "llm", "degraded", _ms_since(t0),
                detail={"provider": llm.name, "reason": "no LLM provider configured"},
            )

        result = await asyncio.wait_for(
            llm.provider.generate(
                "Reply with the single word: OK", max_tokens=10, temperature=0.0,
            ),
            timeout=_LLM_TIMEOUT,
        )

        if not result:
            # Provider's generate() returns None on transient errors; build
            # a deeper failure detail from what we can see locally.
            return CheckResult(
                "llm", "failed", _ms_since(t0),
                detail=_llm_deep_diag(llm, response=None),
                error="provider returned empty/None — see detail.config",
            )
        return CheckResult(
            "llm", "ok", _ms_since(t0),
            detail={
                "provider": llm.name,
                "response_preview": str(result)[:120],
            },
        )
    except asyncio.TimeoutError:
        return CheckResult(
            "llm", "failed", _ms_since(t0),
            detail=_llm_deep_diag(get_llm() if 'llm' not in dir() else llm,
                                  response="timeout"),
            error=f"generation timed out after {_LLM_TIMEOUT}s",
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "llm", "failed", _ms_since(t0),
            detail={"exception_type": type(exc).__name__},
            error=str(exc)[:200],
        )


def _llm_deep_diag(llm: Any, *, response: Any) -> dict[str, Any]:
    """Non-sensitive snapshot of LLM config for failure diagnostics."""
    from app.config import get_settings
    settings = get_settings()
    out: dict[str, Any] = {
        "provider": getattr(llm, "name", "?"),
        "enabled": getattr(llm, "enabled", False),
        "configured_provider": settings.llm_provider,
    }
    if settings.llm_provider == "ollama":
        out["ollama_base_url"] = settings.ollama_base_url
        out["ollama_model"] = settings.ollama_model
        out["hint"] = (
            "Verify the Ollama service is reachable from this container "
            "and the model is pulled: docker compose exec reclio-ollama "
            "ollama pull " + str(settings.ollama_model)
        )
    elif settings.llm_provider == "claude":
        key = settings.anthropic_api_key or ""
        out["anthropic_api_key_present"] = bool(key)
        out["anthropic_api_key_length"] = len(key)
        out["claude_model"] = settings.claude_model
        if not key:
            out["hint"] = "ANTHROPIC_API_KEY missing — set it and restart."
        else:
            out["hint"] = ("Key is present; Anthropic API may be down or "
                           "the key may be invalid for the selected model.")
    elif settings.llm_provider == "openai":
        key = settings.openai_api_key or ""
        out["openai_api_key_present"] = bool(key)
        out["openai_api_key_length"] = len(key)
        out["openai_model"] = settings.openai_model
        if not key:
            out["hint"] = "OPENAI_API_KEY missing — set it and restart."
    out["last_response"] = response
    return out


# ---- Snapshot orchestration --------------------------------------

async def run_health_checks() -> HealthSnapshot:
    """Run all checks in parallel, record the snapshot, log transitions."""
    t0 = time.monotonic()
    results = await asyncio.gather(
        _check_database(),
        _check_trakt(),
        _check_tmdb(),
        _check_recombee(),
        _check_llm(),
        return_exceptions=False,
    )
    checks = {r.name: r for r in results}

    # Overall: failed if any failed; degraded if any degraded; else ok.
    statuses = [r.status for r in results]
    if any(s == "failed" for s in statuses):
        overall = "failed"
    elif any(s == "degraded" for s in statuses):
        overall = "degraded"
    else:
        overall = "ok"

    snap = HealthSnapshot(
        timestamp=datetime.utcnow(),
        overall=overall,
        elapsed_ms=_ms_since(t0),
        checks=checks,
    )

    async with _history_lock:
        global _last_snapshot
        prev = _last_snapshot
        _last_snapshot = snap
        _history.append(snap)

    _log_transitions(snap, prev)
    return snap


def _log_transitions(snap: HealthSnapshot, prev: Optional[HealthSnapshot]) -> None:
    """Quiet on healthy. Loud on degradation. Recovery shows up at INFO."""
    for name, result in snap.checks.items():
        prev_status = (
            prev.checks[name].status
            if prev is not None and name in prev.checks
            else "ok"  # boot-time assumption — first failed check WILL log
        )
        cur = result.status

        if cur == prev_status == "ok":
            # Healthy and unchanged — DEBUG only, never log at INFO.
            logger.debug("health_check: %s ok (%dms)", name, result.elapsed_ms)
            continue

        if cur == "ok" and prev_status != "ok":
            logger.info(
                "health_check: %s RECOVERED (was %s, now ok in %dms)",
                name, prev_status, result.elapsed_ms,
            )
            continue

        # Either we just went bad, or we're still bad. Loud the first time.
        if cur in ("failed", "degraded"):
            level = logger.warning if cur == "failed" else logger.info
            verb = "FAILED" if cur == "failed" else "DEGRADED"
            same = (cur == prev_status)
            if same:
                # Don't spam. Log once per snapshot at DEBUG so it's still
                # in the rolling buffer for /admin/health/history.
                logger.debug(
                    "health_check: %s still %s — %s",
                    name, cur, result.error,
                )
                continue

            # Compose the deep-dive log line. cap detail at ~1KB.
            detail_str = json.dumps(result.detail, default=str)[:1000]
            level(
                "health_check: %s %s (was %s, %dms) — %s | detail=%s",
                name, verb, prev_status, result.elapsed_ms,
                result.error, detail_str,
            )
