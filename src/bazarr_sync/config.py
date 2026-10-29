"""Explicit configuration and shared-storage path boundaries."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit


@dataclass
class Config:
    data_dir: Path = Path("./data")
    media_roots: list[Path] = field(default_factory=list)
    path_mappings: dict[str, str] = field(default_factory=dict)
    bazarr_url: str = ""
    bazarr_api_key: str = field(default="", repr=False)
    host: str = "0.0.0.0"
    port: int = 8080
    public_origin: str = ""
    backend: str = "disabled"
    model: str = "small.en"
    threads: int = 2

    @classmethod
    def from_env(cls):
        roots = json.loads(os.getenv("BBS_MEDIA_ROOTS", "[]"))
        mappings = json.loads(os.getenv("BBS_PATH_MAPPINGS", "{}"))
        if not isinstance(roots, list) or any(not isinstance(p, str) or not p for p in roots):
            raise ValueError("BBS_MEDIA_ROOTS must be a JSON array of nonempty path strings")
        if not isinstance(mappings, dict) or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v for k, v in mappings.items()):
            raise ValueError("BBS_PATH_MAPPINGS must be a JSON object of nonempty path strings")
        cfg = cls(
            data_dir=Path(os.getenv("BBS_DATA_DIR", "./data")),
            media_roots=[Path(p).resolve() for p in roots],
            path_mappings=mappings,
            bazarr_url=os.getenv("BBS_BAZARR_URL", ""),
            bazarr_api_key=os.getenv("BBS_BAZARR_API_KEY", ""),
            host=os.getenv("BBS_HOST", "0.0.0.0"), port=int(os.getenv("BBS_PORT", "8080")),
            public_origin=os.getenv("BBS_PUBLIC_ORIGIN", "").rstrip("/"),
            backend=os.getenv("BBS_BACKEND", "disabled"), model=os.getenv("BBS_MODEL", "small.en"),
            threads=int(os.getenv("BBS_THREADS", "2")),
        )
        if cfg.backend not in {"disabled", "faster-whisper"}:
            raise ValueError("BBS_BACKEND must be disabled or faster-whisper")
        if not 1 <= cfg.threads <= 128:
            raise ValueError("BBS_THREADS outside supported range")
        if cfg.public_origin:
            parsed = urlsplit(cfg.public_origin)
            if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path
                    or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
                raise ValueError("BBS_PUBLIC_ORIGIN must be an http(s) origin")
        return cfg

    def map_path(self, source: str, *, must_exist: bool = True, apply_mapping: bool = True) -> Path:
        if not isinstance(source, str) or not source or "\x00" in source:
            raise ValueError("A nonempty file path is required")
        normalized = source.replace("\\", "/")
        if ".." in PurePosixPath(normalized).parts:
            raise ValueError("Parent path traversal is not allowed")
        mapped = normalized
        if apply_mapping:
            for before, after in sorted(self.path_mappings.items(), key=lambda x: len(x[0]), reverse=True):
                prefix = before.replace("\\", "/").rstrip("/")
                if normalized == prefix or normalized.startswith(prefix + "/"):
                    mapped = after.rstrip("/\\") + normalized[len(prefix):]
                    break
        path = Path(mapped).resolve()
        if not any(path.is_relative_to(Path(root).resolve()) for root in self.media_roots):
            raise ValueError("File is outside configured media roots")
        if must_exist and not path.is_file():
            raise ValueError("Mapped file is missing or inaccessible")
        return path
