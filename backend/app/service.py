from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .job_store import JobStore
from .settings import settings

logger = logging.getLogger(__name__)


def solve_and_verify(
    payload: dict[str, Any], *, time_limit_seconds: float | None = None,
    engine: str = "baseline",
) -> dict[str, Any]:
    # Imported lazily so the API can expose a useful health response if a native
    # solver dependency is unavailable during deployment.
    from .solver import solve_payload
    from .verifier import verify_solution

    result = solve_payload(
        payload,
        time_limit_seconds=time_limit_seconds
        if time_limit_seconds is not None
        else settings.default_time_limit_seconds,
        engine=engine,
    )
    # The parser defaults the cutting kerf to 0 (field MOM export and the legacy
    # nesting software both plan cut lengths kerf-free) while the verifier's own
    # fallback is 10 mm.  When the payload omits BladeMargin the two disagree and
    # the verifier would recompute kerf/remainder against a kerf the solver never
    # used, so pin the verifier to the same default the solver built with.
    verify_payload = payload
    _has_blade = (
        isinstance(payload.get("NestParam"), dict)
        and "BladeMargin" in payload["NestParam"]
    ) or "BladeMargin" in payload
    if not _has_blade:
        from .domain import DEFAULT_BLADE_MARGIN_MM

        verify_payload = dict(payload)
        verify_payload["NestParam"] = {
            **(payload.get("NestParam") or {}),
            "BladeMargin": DEFAULT_BLADE_MARGIN_MM,
        }
    result["verification"] = verify_solution(verify_payload, result)
    if not result["verification"].get("passed", False):
        result["status"] = "verification_failed"
    return result


class JobManager:
    def __init__(self) -> None:
        self.store = JobStore(settings.data_dir / "nesting_jobs.sqlite3")
        self.executor = ThreadPoolExecutor(
            max_workers=settings.max_workers,
            thread_name_prefix="nesting-solver",
        )

    def submit(
        self, payload: dict[str, Any], *, time_limit_seconds: float | None = None
    ) -> dict[str, Any]:
        record = self.store.create(payload)
        self.executor.submit(
            self._execute, record["job_id"], payload, time_limit_seconds
        )
        return record

    def _execute(
        self,
        job_id: str,
        payload: dict[str, Any],
        time_limit_seconds: float | None,
    ) -> None:
        self.store.update(job_id, status="running")
        started = time.monotonic()
        try:
            result = solve_and_verify(
                payload, time_limit_seconds=time_limit_seconds
            )
        except Exception as exc:  # noqa: BLE001 - stored and surfaced to caller
            logger.exception("nesting job %s failed", job_id)
            self.store.update(
                job_id, status="failed", error_message=f"{type(exc).__name__}: {exc}"
            )
            return
        status = (
            "completed"
            if result.get("verification", {}).get("passed", False)
            else "verification_failed"
        )
        group_status: dict[str, int] = {}
        for group in result.get("groups", []):
            key = str(group.get("metrics", {}).get("solve_status", "UNKNOWN"))
            group_status[key] = group_status.get(key, 0) + 1
        logger.info(
            "nesting job %s %s in %.2fs group_status=%s",
            job_id,
            status,
            time.monotonic() - started,
            group_status,
        )
        self.store.update(job_id, status=status, result=result)


job_manager = JobManager()
