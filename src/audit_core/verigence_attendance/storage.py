from __future__ import annotations

from functools import lru_cache

from audit_core.verigence_attendance.settings import get_settings


class AttendanceStorageError(RuntimeError):
    pass


class AttendanceStorage:
    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings

    def _client(self):
        import boto3

        return boto3.client(
            "s3",
            endpoint_url=self._settings.storage_endpoint,
            aws_access_key_id=self._settings.storage_access_key_id,
            aws_secret_access_key=self._settings.storage_secret_access_key,
            region_name=self._settings.storage_region,
        )

    def put(self, *, object_key: str, data: bytes, content_type: str) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self._client().put_object(
                Bucket=self._settings.storage_bucket,
                Key=object_key,
                Body=data,
                ContentType=content_type,
            )
        except (BotoCoreError, ClientError) as exc:
            raise AttendanceStorageError("Attendance evidence storage is unavailable") from exc

    def presign(self, *, object_key: str, expires_seconds: int = 900) -> str:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return str(
                self._client().generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self._settings.storage_bucket, "Key": object_key},
                    ExpiresIn=expires_seconds,
                )
            )
        except (BotoCoreError, ClientError) as exc:
            raise AttendanceStorageError("Attendance evidence storage is unavailable") from exc


@lru_cache
def storage() -> AttendanceStorage:
    return AttendanceStorage()
