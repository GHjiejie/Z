from __future__ import annotations

import os
from datetime import timedelta
from typing import Any, Optional

from packages.knowledge.errors import KnowledgeStorageError
from packages.knowledge.ports import ObjectMetadata, UploadAuthorization

from .credentials import create_oss_credentials_provider


class AliyunOSSObjectStorage:
    provider = "aliyun_oss"

    def __init__(self, bucket: str, region: str, endpoint: Optional[str] = None):
        if not bucket or not region:
            raise ValueError("ALIYUN_OSS_BUCKET and ALIYUN_OSS_REGION are required")
        self.bucket = bucket
        self.region = region
        self.endpoint = endpoint
        self._oss: Any = None
        self._sdk_client: Any = None

    @classmethod
    def from_environment(cls) -> "AliyunOSSObjectStorage":
        region = os.getenv("ALIYUN_OSS_REGION", "cn-beijing")
        bucket = os.getenv("ALIYUN_OSS_BUCKET", "jie-agent-file")
        use_internal = os.getenv("ALIYUN_OSS_USE_INTERNAL_ENDPOINT", "false").lower() == "true"
        if use_internal:
            endpoint = os.getenv(
                "ALIYUN_OSS_INTERNAL_ENDPOINT", f"https://oss-{region}-internal.aliyuncs.com"
            )
        else:
            endpoint = os.getenv("ALIYUN_OSS_ENDPOINT", f"https://oss-{region}.aliyuncs.com")
        return cls(bucket=bucket, region=region, endpoint=endpoint)

    def canonical_uri(self, object_key: str) -> str:
        return f"oss://{self.bucket}/{object_key}"

    def create_upload_authorization(
        self, object_key: str, content_type: str, expires_seconds: int = 900
    ) -> UploadAuthorization:
        oss, client = self._client()
        try:
            result = client.presign(
                oss.PutObjectRequest(
                    bucket=self.bucket,
                    key=object_key,
                    content_type=content_type,
                ),
                expires=timedelta(seconds=expires_seconds),
            )
        except Exception as exc:
            raise KnowledgeStorageError(f"Unable to create OSS upload authorization: {exc}") from exc
        return UploadAuthorization(
            method=result.method,
            url=result.url,
            expires_at=result.expiration.isoformat(),
            headers=dict(result.signed_headers or {}),
        )

    def put_content(self, object_key: str, content: bytes, content_type: str) -> ObjectMetadata:
        oss, client = self._client()
        try:
            result = client.put_object(
                oss.PutObjectRequest(
                    bucket=self.bucket,
                    key=object_key,
                    body=content,
                    content_type=content_type,
                )
            )
        except Exception as exc:
            raise KnowledgeStorageError(f"Unable to upload OSS object: {exc}") from exc
        return ObjectMetadata(
            bucket=self.bucket,
            region=self.region,
            object_key=object_key,
            size_bytes=len(content),
            content_type=content_type,
            etag=getattr(result, "etag", None),
            version_id=getattr(result, "version_id", None),
            storage_class="STANDARD",
        )

    def head_object(self, object_key: str, version_id: Optional[str] = None) -> ObjectMetadata:
        oss, client = self._client()
        try:
            result = client.head_object(
                oss.HeadObjectRequest(
                    bucket=self.bucket,
                    key=object_key,
                    version_id=version_id,
                )
            )
        except Exception as exc:
            raise KnowledgeStorageError(f"Unable to read OSS object metadata: {exc}") from exc
        return ObjectMetadata(
            bucket=self.bucket,
            region=self.region,
            object_key=object_key,
            size_bytes=int(result.content_length),
            content_type=result.content_type or "application/octet-stream",
            etag=result.etag,
            version_id=result.version_id,
            storage_class=result.storage_class,
        )

    def get_content(self, object_key: str, version_id: Optional[str] = None) -> bytes:
        oss, client = self._client()
        try:
            result = client.get_object(
                oss.GetObjectRequest(
                    bucket=self.bucket,
                    key=object_key,
                    version_id=version_id,
                )
            )
            return result.body.read()
        except Exception as exc:
            raise KnowledgeStorageError(f"Unable to download OSS object: {exc}") from exc

    def create_download_url(
        self, object_key: str, version_id: Optional[str] = None, expires_seconds: int = 300
    ) -> Optional[str]:
        oss, client = self._client()
        try:
            result = client.presign(
                oss.GetObjectRequest(
                    bucket=self.bucket,
                    key=object_key,
                    version_id=version_id,
                ),
                expires=timedelta(seconds=expires_seconds),
            )
            return result.url
        except Exception as exc:
            raise KnowledgeStorageError(f"Unable to create OSS download URL: {exc}") from exc

    def _client(self):
        if self._sdk_client is not None:
            return self._oss, self._sdk_client
        try:
            import alibabacloud_oss_v2 as oss
        except ImportError as exc:
            raise KnowledgeStorageError(
                "OSS storage is configured but alibabacloud-oss-v2 is not installed"
            ) from exc
        configuration = oss.config.load_default()
        configuration.credentials_provider = create_oss_credentials_provider(oss)
        configuration.region = self.region
        if self.endpoint:
            configuration.endpoint = self.endpoint
        self._oss = oss
        self._sdk_client = oss.Client(configuration)
        return self._oss, self._sdk_client
