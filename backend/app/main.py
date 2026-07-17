from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .job_store import JobNotFoundError
from .service import job_manager, solve_and_verify
from .settings import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("nesting.api")

app = FastAPI(
    title="蛇形管一维排料服务",
    description="锅炉过热器、再热器、省煤器管段联合切割与拼接优化",
    version=__version__,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_allow_origins),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _solve_status_summary(result: dict[str, Any]) -> dict[str, int]:
    """Count how many groups landed in each solver status, for ops visibility."""

    summary: dict[str, int] = {}
    for group in result.get("groups", []):
        status = str(group.get("metrics", {}).get("solve_status", "UNKNOWN"))
        summary[status] = summary.get(status, 0) + 1
    return summary


@app.get("/api/v1/health")
def health() -> dict[str, Any]:
    solver_available = True
    solver_error: str | None = None
    try:
        import pyscipopt  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        solver_available = False
        solver_error = str(exc)
    return {
        "status": "ok" if solver_available else "degraded",
        "version": __version__,
        "solver": "SCIP" if solver_available else "deterministic-fallback",
        "solver_error": solver_error,
    }


@app.post("/api/v1/solve")
async def solve(
    payload: Annotated[dict[str, Any], Body()],
    time_limit_seconds: Annotated[
        float | None, Query(ge=1, le=3600)
    ] = None,
    engine: Annotated[str, Query(pattern="^(baseline|route3)$")] = "baseline",
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = await run_in_threadpool(
            solve_and_verify,
            payload,
            time_limit_seconds=time_limit_seconds,
            engine=engine,
        )
    except ValueError as exc:
        logger.warning(
            "solve rejected after %.2fs: %s", time.monotonic() - started, exc
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    logger.info(
        "solve done in %.2fs engine=%s status=%s verified=%s group_status=%s",
        time.monotonic() - started,
        engine,
        result.get("status"),
        result.get("verification", {}).get("passed"),
        _solve_status_summary(result),
    )
    return result


@app.post("/api/v1/jobs", status_code=202)
def create_job(
    payload: Annotated[dict[str, Any], Body()],
    time_limit_seconds: Annotated[
        float | None, Query(ge=1, le=3600)
    ] = None,
) -> dict[str, Any]:
    return job_manager.submit(payload, time_limit_seconds=time_limit_seconds)


@app.get("/api/v1/jobs")
def list_jobs(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    return {"jobs": job_manager.store.list_recent(limit)}


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    try:
        return job_manager.store.get(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="排料任务不存在") from exc


@app.post("/api/v1/compare-log", status_code=201)
def compare_log(payload: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    """把一次「本系统 vs 旧软件」对比结果追加到 JSONL，便于事后分析差距。

    前端在每次求解完成后调用；后端只负责落盘，不做校验，字段由前端决定。
    """

    record = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    log_path = settings.data_dir / "compare_log.jsonl"
    line = json.dumps(record, ensure_ascii=False)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    logger.info(
        "compare-log sample=%s sys(cut=%s,weld=%s,util=%s) legacy(cut=%s,weld=%s,util=%s)",
        payload.get("sample_id"),
        (payload.get("system") or {}).get("cuttingPatternTypes"),
        (payload.get("system") or {}).get("weldingPatternTypes"),
        (payload.get("system") or {}).get("utilization"),
        (payload.get("legacy") or {}).get("cuttingPatternTypes"),
        (payload.get("legacy") or {}).get("weldingPatternTypes"),
        (payload.get("legacy") or {}).get("utilization"),
    )
    return {"ok": True, "path": str(log_path)}


@app.get("/")
def index() -> dict[str, Any]:
    # 前端已迁移到独立的 Next 工程（frontend-next，开发默认 http://localhost:3001）。
    # 后端只提供 JSON API。
    return {
        "service": "蛇形管一维排料服务",
        "version": __version__,
        "frontend": "见 frontend-next（vinext dev）",
        "endpoints": ["/api/v1/health", "/api/v1/solve", "/api/v1/jobs", "/api/v1/compare-log"],
    }
