"""Local/remote artifact publishing strategy for unified responses."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from rdx.runtime_paths import artifacts_dir

from .contracts import make_artifact_from_path, make_artifact_from_url


class ArtifactPublisher:
    def __init__(self) -> None:
        self.mode = str(os.environ.get("RDX_ARTIFACT_MODE", "local")).strip().lower()
        self.remote_min_bytes = int(os.environ.get("RDX_REMOTE_ARTIFACT_MIN_BYTES", "65536"))
        self._s3_endpoint = os.environ.get("RDX_S3_ENDPOINT")
        self._s3_bucket = os.environ.get("RDX_S3_BUCKET")
        self._s3_region = os.environ.get("RDX_S3_REGION")
        self._s3_access_key = os.environ.get("RDX_S3_ACCESS_KEY")
        self._s3_secret_key = os.environ.get("RDX_S3_SECRET_KEY")
        self._s3_presign_seconds = int(os.environ.get("RDX_S3_PRESIGN_SECONDS", "86400"))

    def _should_upload_remote(self, path: Path, *, remote: bool) -> bool:
        if not remote:
            return False
        if self.mode != "remote":
            return False
        if not path.exists() or not path.is_file():
            return False
        return int(path.stat().st_size) >= self.remote_min_bytes

    def _upload_s3(self, path: Path) -> Optional[str]:
        if not all([self._s3_bucket, self._s3_access_key, self._s3_secret_key]):
            return None
        try:
            import boto3  # type: ignore[import-not-found]
        except Exception:
            return None

        client = boto3.client(
            "s3",
            endpoint_url=self._s3_endpoint,
            region_name=self._s3_region,
            aws_access_key_id=self._s3_access_key,
            aws_secret_access_key=self._s3_secret_key,
        )
        key = f"rdx/{time.strftime('%Y/%m/%d')}/{path.name}"
        client.upload_file(str(path), self._s3_bucket, key)
        return str(
            client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._s3_bucket, "Key": key},
                ExpiresIn=self._s3_presign_seconds,
            ),
        )

    async def publish_candidates(
        self,
        candidates: List[Dict[str, Any]],
        *,
        remote: bool,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in candidates:
            if not isinstance(item, dict):
                continue
            if item.get("url"):
                url = str(item.get("url"))
                if url in seen:
                    continue
                seen.add(url)
                out.append(
                    make_artifact_from_url(
                        url,
                        artifact_type=str(item.get("type", "file")),
                        metadata=dict(item.get("metadata") or {}),
                    ),
                )
                continue

            path_value = str(item.get("path") or "").strip()
            if not path_value:
                continue
            p = Path(path_value)
            key = f"path::{p}"
            if key in seen:
                continue
            seen.add(key)

            if self._should_upload_remote(p, remote=remote):
                uploaded_url = self._upload_s3(p)
                if uploaded_url:
                    out.append(
                        make_artifact_from_url(
                            uploaded_url,
                            artifact_type=str(item.get("type", "file")),
                            metadata={
                                **dict(item.get("metadata") or {}),
                                "source_path": str(p),
                            },
                            size_bytes=int(p.stat().st_size),
                            storage_backend="s3",
                        ),
                    )
                    continue
            out.append(
                make_artifact_from_path(
                    str(p),
                    artifact_type=str(item.get("type", "file")),
                    metadata=dict(item.get("metadata") or {}),
                    storage_backend="local",
                ),
            )
        return out


def default_artifact_root() -> Path:
    return Path(os.environ.get("RDX_ARTIFACT_DIR", str(artifacts_dir()))).resolve()

