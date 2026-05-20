"""Content-addressable artifact store.

Stores raw bytes as art:<sha256-prefix> with metadata sidecar.
Directory: state/artifacts/
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from schemas import Artifact

STATE_DIR = Path(__file__).parent / "state"
ARTIFACTS_DIR = STATE_DIR / "artifacts"


class ArtifactStore:
    def __init__(self):
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes, content_type: str, source: str, descriptor: str) -> Artifact:
        sha = hashlib.sha256(data).hexdigest()[:16]
        art_id = f"art:{sha}"
        bin_path = ARTIFACTS_DIR / f"{art_id}.bin"
        meta_path = ARTIFACTS_DIR / f"{art_id}.json"

        bin_path.write_bytes(data)
        artifact = Artifact(
            id=art_id,
            content_type=content_type,
            size_bytes=len(data),
            source=source,
            descriptor=descriptor,
        )
        meta_path.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
        return artifact

    def get_bytes(self, art_id: str) -> bytes | None:
        bin_path = ARTIFACTS_DIR / f"{art_id}.bin"
        if bin_path.exists():
            return bin_path.read_bytes()
        return None

    def get_meta(self, art_id: str) -> Artifact | None:
        meta_path = ARTIFACTS_DIR / f"{art_id}.json"
        if meta_path.exists():
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            return Artifact(**data)
        return None

    def exists(self, art_id: str) -> bool:
        return (ARTIFACTS_DIR / f"{art_id}.bin").exists()
