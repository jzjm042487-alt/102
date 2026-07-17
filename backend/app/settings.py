from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path
    data_dir: Path
    max_workers: int
    default_time_limit_seconds: float
    cors_allow_origins: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "Settings":
        project_root = Path(__file__).resolve().parents[2]
        data_dir = Path(
            os.getenv("NESTING_DATA_DIR", str(project_root / "data"))
        ).resolve()
        max_workers = max(1, int(os.getenv("NESTING_MAX_WORKERS", "2")))
        # Real production groups (e.g. tightly supplied ones) need well over the
        # legacy 30s to reach a lexicographic optimum; default to a headroom that
        # matches observed solve times, still overridable per deployment/request.
        time_limit = max(
            1.0, float(os.getenv("NESTING_TIME_LIMIT_SECONDS", "120"))
        )
        # Comma-separated origins; "*" (default) allows any origin.  Narrow this
        # in production when the frontend is served from a known host.
        origins_raw = os.getenv("NESTING_CORS_ALLOW_ORIGINS", "*")
        cors_allow_origins = tuple(
            origin.strip() for origin in origins_raw.split(",") if origin.strip()
        ) or ("*",)
        data_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            project_root=project_root,
            data_dir=data_dir,
            max_workers=max_workers,
            default_time_limit_seconds=time_limit,
            cors_allow_origins=cors_allow_origins,
        )


settings = Settings.from_env()
