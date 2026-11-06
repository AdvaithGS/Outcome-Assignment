"""L2: cache for expensive non-LLM work (OCR text, rendered page images, parsed representations).

Key = file sha256 + stage + stage_version + params. Unlike LLM requests, a code change in a stage
does not show up in its inputs, so each stage has a manual `version` constant: bump it when the
stage's output would change. Files are written atomically (temp file + rename), so concurrent
workers never see half-written entries. In production this doubles as stage checkpointing: a
restarted job does not redo OCR.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

from app.hashing import canonical_json, sha256_hex


class ArtifactCache:
    def __init__(self, root: str | Path, *, enabled: bool = True):
        self.root = Path(root)
        self.enabled = enabled

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ArtifactCache":
        env = os.environ if env is None else env
        return cls(env.get("ARTIFACT_CACHE_DIR", ".cache/artifacts"),
                   enabled=env.get("ARTIFACT_CACHE", "on") != "off")

    def path_for(self, *, file_sha: str, stage: str, version: int, params: Mapping[str, Any], ext: str) -> Path:
        params_hash = sha256_hex(canonical_json(dict(params)))[:12]
        return self.root / stage / f"v{version}" / file_sha[:2] / f"{file_sha}-{params_hash}{ext}"

    def get_or_compute_bytes(self, *, file_sha: str, stage: str, version: int, params: Mapping[str, Any],
                             compute: Callable[[], bytes], ext: str = ".bin") -> bytes:
        if not self.enabled:
            return compute()
        path = self.path_for(file_sha=file_sha, stage=stage, version=version, params=params, ext=ext)
        if path.exists():
            return path.read_bytes()
        data = compute()
        _atomic_write(path, data)
        return data

    def get_or_compute_json(self, *, file_sha: str, stage: str, version: int, params: Mapping[str, Any],
                            compute: Callable[[], Any]) -> Any:
        raw = self.get_or_compute_bytes(
            file_sha=file_sha, stage=stage, version=version, params=params, ext=".json",
            compute=lambda: json.dumps(compute(), ensure_ascii=False).encode("utf-8"),
        )
        return json.loads(raw)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
