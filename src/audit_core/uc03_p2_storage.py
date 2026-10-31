"""S3-compatible storage adapter for UC03 Phase 2 document intake.

Uses the same storage technology already present in Audit Core for vehicle
photos. Phase 2 may use dedicated AUDIT_CORE_P2_STORAGE_* settings; when
those are absent it reuses the existing photo-storage endpoint/credentials/
bucket under an isolated p2-documents/ object prefix. No DI storage changes
are required.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


class P2DocumentStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class P2DocumentStorageSettings:
    endpoint_url: str
    access_key_id: str
    secret_access_key: str
    bucket: str
    region: str = "auto"


class P2DocumentStorage:
    def __init__(self, settings: P2DocumentStorageSettings) -> None:
        self._settings = settings

    def _client(self):
        import boto3

        return boto3.client(
            "s3",
            endpoint_url=self._settings.endpoint_url,
            aws_access_key_id=self._settings.access_key_id,
            aws_secret_access_key=self._settings.secret_access_key,
            region_name=self._settings.region,
        )

    @property
    def bucket(self) -> str:
        return self._settings.bucket

    def probe_bucket(self, *, timeout_seconds: float = 5.0) -> None:
        """HEAD the bucket once with short timeouts and no retries (System Health's live
        probe, 2026-10-31). Raises P2DocumentStorageError when it does not answer."""
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError

        client = boto3.client(
            "s3",
            endpoint_url=self._settings.endpoint_url,
            aws_access_key_id=self._settings.access_key_id,
            aws_secret_access_key=self._settings.secret_access_key,
            region_name=self._settings.region,
            config=Config(connect_timeout=min(timeout_seconds, 3.0), read_timeout=timeout_seconds,
                          retries={"max_attempts": 1, "mode": "standard"}),
        )
        try:
            client.head_bucket(Bucket=self._settings.bucket)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code") or exc.response.get("ResponseMetadata", {}).get(
                "HTTPStatusCode")
            raise P2DocumentStorageError(f"R2 answered {code}") from exc
        except BotoCoreError as exc:
            raise P2DocumentStorageError(f"R2 did not answer ({type(exc).__name__})") from exc

    def presign_put(self, object_key: str, *, content_type: str, expires_seconds: int = 900) -> str:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return str(
                self._client().generate_presigned_url(
                    "put_object",
                    Params={
                        "Bucket": self._settings.bucket,
                        "Key": object_key,
                        "ContentType": content_type,
                    },
                    ExpiresIn=expires_seconds,
                )
            )
        except (BotoCoreError, ClientError) as exc:
            raise P2DocumentStorageError(f"Failed to sign upload for {object_key}") from exc

    def put_object(self, object_key: str, data: bytes, *, content_type: str) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self._client().put_object(
                Bucket=self._settings.bucket,
                Key=object_key,
                Body=data,
                ContentType=content_type,
            )
        except (BotoCoreError, ClientError) as exc:
            raise P2DocumentStorageError(f"Failed to store {object_key}") from exc

    def get_object(self, object_key: str) -> bytes:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            response = self._client().get_object(Bucket=self._settings.bucket, Key=object_key)
            return bytes(response["Body"].read())
        except (BotoCoreError, ClientError, KeyError) as exc:
            raise P2DocumentStorageError(f"Failed to read {object_key}") from exc

    def head_object(self, object_key: str) -> dict[str, object]:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            response = self._client().head_object(Bucket=self._settings.bucket, Key=object_key)
            return {
                "contentLength": int(response.get("ContentLength") or 0),
                "contentType": str(response.get("ContentType") or ""),
                "etag": str(response.get("ETag") or "").strip('"'),
            }
        except (BotoCoreError, ClientError) as exc:
            raise P2DocumentStorageError(f"Failed to inspect {object_key}") from exc

    def delete_objects(self, object_keys: list[str]) -> int:
        """Delete the given keys (a key already gone is not an error). Returns
        how many keys were sent for deletion."""
        from botocore.exceptions import BotoCoreError, ClientError

        keys = sorted({key for key in object_keys if key})
        try:
            client = self._client()
            for offset in range(0, len(keys), 1000):
                batch = keys[offset : offset + 1000]
                response = client.delete_objects(
                    Bucket=self._settings.bucket,
                    Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
                )
                errors = response.get("Errors") or []
                if errors:
                    raise P2DocumentStorageError(
                        f"Failed to delete {len(errors)} of {len(batch)} stored files"
                    )
        except (BotoCoreError, ClientError) as exc:
            raise P2DocumentStorageError("Failed to delete stored files") from exc
        return len(keys)

    def list_keys(self, prefix: str) -> list[str]:
        """Every key under ``prefix`` (which must end with "/")."""
        from botocore.exceptions import BotoCoreError, ClientError

        if not prefix.endswith("/"):
            raise ValueError("A storage prefix must end with '/'")
        keys: list[str] = []
        try:
            paginator = self._client().get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._settings.bucket, Prefix=prefix):
                keys.extend(str(item["Key"]) for item in page.get("Contents") or [])
        except (BotoCoreError, ClientError) as exc:
            raise P2DocumentStorageError(f"Failed to list {prefix}") from exc
        return keys

    def presign_get(self, object_key: str, *, expires_seconds: int = 1800) -> str:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return str(
                self._client().generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self._settings.bucket, "Key": object_key},
                    ExpiresIn=expires_seconds,
                )
            )
        except (BotoCoreError, ClientError) as exc:
            raise P2DocumentStorageError(f"Failed to sign read for {object_key}") from exc


@lru_cache
def get_p2_document_storage() -> P2DocumentStorage:
    endpoint_url = (
        os.environ.get("AUDIT_CORE_P2_STORAGE_ENDPOINT", "").strip()
        or os.environ.get("AUDIT_CORE_PHOTO_STORAGE_ENDPOINT", "").strip()
    )
    access_key_id = (
        os.environ.get("AUDIT_CORE_P2_STORAGE_ACCESS_KEY_ID", "").strip()
        or os.environ.get("AUDIT_CORE_PHOTO_STORAGE_ACCESS_KEY_ID", "").strip()
    )
    secret_access_key = (
        os.environ.get("AUDIT_CORE_P2_STORAGE_SECRET_ACCESS_KEY", "").strip()
        or os.environ.get("AUDIT_CORE_PHOTO_STORAGE_SECRET_ACCESS_KEY", "").strip()
    )
    bucket = (
        os.environ.get("AUDIT_CORE_P2_STORAGE_BUCKET", "").strip()
        or os.environ.get("AUDIT_CORE_PHOTO_STORAGE_BUCKET", "").strip()
    )
    region = (
        os.environ.get("AUDIT_CORE_P2_STORAGE_REGION", "").strip()
        or os.environ.get("AUDIT_CORE_PHOTO_STORAGE_REGION", "auto").strip()
        or "auto"
    )
    if not endpoint_url or not access_key_id or not secret_access_key or not bucket:
        raise RuntimeError("Phase 2 document storage is not configured")
    return P2DocumentStorage(
        P2DocumentStorageSettings(
            endpoint_url=endpoint_url,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            bucket=bucket,
            region=region,
        )
    )
