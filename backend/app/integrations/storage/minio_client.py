"""MinIO (S3-compatible) object storage — the actual bytes behind a
`documents` row's `storage_uri`. Bucket is created on first use if it
doesn't exist yet (idempotent — real MinIO/S3 both no-op on an existing
bucket other than raising a benign "already own it" error, caught here).
"""

import hashlib
from functools import lru_cache

import boto3
from botocore.exceptions import ClientError

from app.core.config import get_settings


@lru_cache
def _client():
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.minio_endpoint_url,
        aws_access_key_id=settings.minio_access_key,
        aws_secret_access_key=settings.minio_secret_key,
    )


def _ensure_bucket(bucket: str) -> None:
    client = _client()
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError:
        client.create_bucket(Bucket=bucket)


def upload_bytes(*, key: str, data: bytes, content_type: str) -> tuple[str, str]:
    """Uploads `data` under `key` in the configured bucket.

    Returns (storage_uri, checksum) — storage_uri is an `s3://bucket/key`
    URI (not a presigned URL, which expires; callers needing a fetchable
    link should generate one from this on demand), checksum is the sha256
    hex digest `documents.checksum` expects for dedupe.
    """
    settings = get_settings()
    _ensure_bucket(settings.minio_bucket)
    checksum = hashlib.sha256(data).hexdigest()
    _client().put_object(Bucket=settings.minio_bucket, Key=key, Body=data, ContentType=content_type)
    return f"s3://{settings.minio_bucket}/{key}", checksum


def get_object_bytes(*, key: str) -> bytes:
    settings = get_settings()
    response = _client().get_object(Bucket=settings.minio_bucket, Key=key)
    return response["Body"].read()
