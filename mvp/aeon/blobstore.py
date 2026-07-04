"""Content-addressed blob store for large result payloads (raw_output, transcripts,
arena HTML). Keeps big blobs OUT of the database — the design rule from
docs/data-and-security.md §2.1: the DB stores a `ref` + sha256, the bytes live here.

The ref **is** the sha256 of the content, so:
  - storage is automatically de-duplicated (identical payloads share one object), and
  - a blob cannot be swapped without breaking the hash → it's an integrity property.

Backends (selected by AEON_BLOB_BACKEND):
  - `fs` (default): sharded files under AEON_BLOB_DIR (local dev / single host).
  - `s3`: S3/MinIO via boto3 (AEON_BLOB_BUCKET, AEON_S3_ENDPOINT, std AWS creds) — prod.
"""
from __future__ import annotations

import hashlib
import os

MVP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_BACKEND = os.environ.get("AEON_BLOB_BACKEND", "fs").lower()
_FS_DIR = os.environ.get("AEON_BLOB_DIR") or os.path.join(MVP_DIR, "blobs")
_S3_BUCKET = os.environ.get("AEON_BLOB_BUCKET")
_S3_PREFIX = os.environ.get("AEON_BLOB_PREFIX", "blobs/")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_text(text: str) -> str:
    return _sha((text or "").encode("utf-8"))


# ---- filesystem backend ----

def _fs_path(ref: str) -> str:
    return os.path.join(_FS_DIR, ref[:2], ref[2:4], ref)


def _fs_put(data: bytes, ref: str) -> None:
    p = _fs_path(ref)
    if os.path.exists(p):                       # content-addressed → already stored
        return
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, p)                          # atomic publish


def _fs_get(ref: str):
    p = _fs_path(ref)
    if not os.path.exists(p):
        return None
    with open(p, "rb") as f:
        return f.read()


# ---- s3 / minio backend ----

def _s3_client():
    import boto3
    return boto3.client("s3", endpoint_url=os.environ.get("AEON_S3_ENDPOINT") or None)


def _s3_key(ref: str) -> str:
    return f"{_S3_PREFIX}{ref[:2]}/{ref[2:4]}/{ref}"


def _s3_put(data: bytes, ref: str) -> None:
    cli = _s3_client()
    try:                                        # dedup: skip if already present
        cli.head_object(Bucket=_S3_BUCKET, Key=_s3_key(ref))
        return
    except Exception:
        pass
    cli.put_object(Bucket=_S3_BUCKET, Key=_s3_key(ref), Body=data)


def _s3_get(ref: str):
    try:
        return _s3_client().get_object(Bucket=_S3_BUCKET, Key=_s3_key(ref))["Body"].read()
    except Exception:
        return None


# ---- public API ----

def put(data) -> str:
    """Store bytes/str content-addressed; returns the sha256 ref (idempotent)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    ref = _sha(data)
    (_s3_put if _BACKEND == "s3" else _fs_put)(data, ref)
    return ref


def get(ref: str):
    if not ref:
        return None
    return (_s3_get if _BACKEND == "s3" else _fs_get)(ref)


def put_text(text: str) -> str:
    return put((text or "").encode("utf-8"))


def get_text(ref: str) -> str:
    data = get(ref)
    return data.decode("utf-8", "replace") if data is not None else ""
