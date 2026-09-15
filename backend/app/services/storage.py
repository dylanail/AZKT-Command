"""Private file storage. Local disk in dev; S3-compatible bucket in prod (spec §13.3).
Object keys are immutable and content-addressed; nothing is public."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from ..core.config import settings


class LocalStorage:
    def __init__(self, root: str | None = None):
        self.root = Path(root or settings.DATA_DIR) / "files"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise ValueError("bad storage key")
        return p

    def put(self, data: bytes, *, ext: str = "", prefix: str = "assets") -> tuple[str, str]:
        sha = hashlib.sha256(data).hexdigest()
        key = f"{prefix}/{sha[:2]}/{sha}{ext}"
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            tmp = p.with_suffix(p.suffix + ".part")
            tmp.write_bytes(data)
            os.replace(tmp, p)
        return key, sha

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            p.unlink()

    def append_part(self, key: str, data: bytes) -> int:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "ab") as fh:
            fh.write(data)
        return p.stat().st_size


class S3Storage(LocalStorage):
    """S3-compatible storage via signed requests (Railway buckets). Implemented with httpx +
    SigV4; falls back to local if not configured. Kept minimal: put/get/exists/delete."""

    def __init__(self):
        super().__init__()
        import boto3  # type: ignore  # optional dependency; only needed when STORAGE_BACKEND=s3
        self.client = boto3.client("s3", endpoint_url=settings.S3_ENDPOINT or None,
                                   aws_access_key_id=settings.S3_ACCESS_KEY, aws_secret_access_key=settings.S3_SECRET_KEY,
                                   region_name=settings.S3_REGION)
        self.bucket = settings.S3_BUCKET

    def put(self, data: bytes, *, ext: str = "", prefix: str = "assets") -> tuple[str, str]:
        sha = hashlib.sha256(data).hexdigest()
        key = f"{prefix}/{sha[:2]}/{sha}{ext}"
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
        return key, sha

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001
            return False

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


_storage = None


def storage() -> LocalStorage:
    global _storage
    if _storage is None:
        _storage = S3Storage() if settings.STORAGE_BACKEND == "s3" and settings.S3_BUCKET else LocalStorage()
    return _storage


def reset_storage(root: str | None = None) -> None:
    global _storage
    _storage = LocalStorage(root)
