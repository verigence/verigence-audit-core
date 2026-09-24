"""vehicle_photo_storage.py — minimal S3-compatible storage for raw Delivery
vehicle photos, deliberately independent of DI.

DI is "Document Intelligence" -- every document that goes through it is
classified and (where a profile exists) extracted. A photo of the delivered
vehicle is not a business document; there is nothing to classify or extract
from it, and it must never enter DI's classify/extract pipeline. Audit-core
has never stored a raw file itself before (every document until now has
gone through DI's own storage layer), so this is new, intentionally small:
a synchronous, S3-compatible client (the same technology DI's own adapter
uses for R2/MinIO) for exactly one purpose -- put a photo, read it back via
a short-lived presigned URL. No streaming, no multi-part uploads, no
classification hooks: a handful of small images per journey.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


class VehiclePhotoStorageError(RuntimeError):
    """Raised when the photo storage backend is unreachable or misconfigured."""


@dataclass(frozen=True)
class VehiclePhotoStorageSettings:
    endpoint_url: str
    access_key_id: str
    secret_access_key: str
    bucket: str
    region: str = "auto"


class VehiclePhotoStorage:
    """Thin synchronous S3-compatible client (Cloudflare R2 / MinIO)."""

    def __init__(self, settings: VehiclePhotoStorageSettings) -> None:
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

    def put_object(self, object_key: str, data: bytes, content_type: str) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self._client().put_object(
                Bucket=self._settings.bucket,
                Key=object_key,
                Body=data,
                ContentType=content_type,
            )
        except (BotoCoreError, ClientError) as exc:
            raise VehiclePhotoStorageError(f"Failed to store {object_key}") from exc

    def get_presigned_url(self, object_key: str, expires_seconds: int = 1800) -> str:
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
            raise VehiclePhotoStorageError(f"Failed to sign {object_key}") from exc


@lru_cache
def get_vehicle_photo_storage() -> VehiclePhotoStorage:
    endpoint_url = os.environ.get("AUDIT_CORE_PHOTO_STORAGE_ENDPOINT", "").strip()
    access_key_id = os.environ.get("AUDIT_CORE_PHOTO_STORAGE_ACCESS_KEY_ID", "").strip()
    secret_access_key = os.environ.get("AUDIT_CORE_PHOTO_STORAGE_SECRET_ACCESS_KEY", "").strip()
    bucket = os.environ.get("AUDIT_CORE_PHOTO_STORAGE_BUCKET", "").strip()
    region = os.environ.get("AUDIT_CORE_PHOTO_STORAGE_REGION", "auto").strip() or "auto"
    if not endpoint_url or not access_key_id or not secret_access_key or not bucket:
        raise RuntimeError("Vehicle photo storage is not configured")
    return VehiclePhotoStorage(
        VehiclePhotoStorageSettings(
            endpoint_url=endpoint_url,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            bucket=bucket,
            region=region,
        )
    )
