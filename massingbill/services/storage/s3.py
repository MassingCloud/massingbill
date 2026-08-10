"""S3-compatible object storage.

An **optional adapter**. The core never imports this module -- it is reached
only through ``storage.get_backend("s3")``, and CI deletes the file and re-runs
the whole suite to prove the product still works without it (SPEC.md 3, 13).

Works against AWS S3 and anything that speaks its API (MinIO, Ceph, Backblaze
B2, Cloudflare R2) via ``s3_endpoint_url``. That matters more than AWS support
does: a contractor who will not put their pay applications in someone else's
cloud can point this at a MinIO box in their own server room and keep every
other property of the system.

Like every backend here, this returns opaque pointers and never a public URL.
A presigned URL would be a bearer token for a financial document, sitting in
whatever proxy log it passed through.
"""

from __future__ import annotations

import hashlib
from typing import Any, BinaryIO

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from massingbill.services.storage.base import StorageBackend, StoragePointer


class S3Storage(StorageBackend):
    name = "s3"

    def __init__(
        self,
        *,
        bucket: str,
        region: str = "",
        endpoint_url: str = "",
        access_key_id: str = "",
        secret_access_key: str = "",
        prefix: str = "massingbill",
    ) -> None:
        if not bucket:
            raise ValueError("S3 storage needs a bucket name (MASSINGBILL_S3_BUCKET).")

        self.bucket = bucket
        self.prefix = prefix.strip("/")

        # Credentials fall back to the ambient chain (instance role, env,
        # ~/.aws) when not configured, which is how this should be run in AWS:
        # a role beats a key pair sitting in an env file.
        session = boto3.session.Session(
            aws_access_key_id=access_key_id or None,
            aws_secret_access_key=secret_access_key or None,
            region_name=region or None,
        )
        self._client = session.client(
            "s3",
            endpoint_url=endpoint_url or None,
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def _full_key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, stream: BinaryIO, *, content_type: str) -> StoragePointer:
        """Store the object, and record what was actually stored.

        The digest is computed here rather than taken from the caller, and the
        size from the bytes rather than from a header. A pointer whose digest
        was supplied by the thing being stored proves nothing.
        """
        payload = stream.read()
        digest = hashlib.sha256(payload).hexdigest()

        self._client.put_object(
            Bucket=self.bucket,
            Key=self._full_key(key),
            Body=payload,
            ContentType=content_type,
            # Server-side encryption is the default rather than an option. A
            # bucket policy may already require it; setting it here means the
            # write does not depend on somebody else having done that.
            ServerSideEncryption="AES256",
            Metadata={"sha256": digest},
        )

        return StoragePointer(
            backend=self.name,
            key=key,
            size=len(payload),
            sha256=digest,
            content_type=content_type,
        )

    def open(self, pointer: StoragePointer) -> BinaryIO:
        import io

        response = self._client.get_object(Bucket=self.bucket, Key=self._full_key(pointer.key))
        payload: bytes = response["Body"].read()

        # Verify on the way out. Object storage is durable, not immutable: a
        # document that no longer matches the digest recorded when it was
        # written is a different document, and a signed waiver whose bytes
        # changed is exactly the case this catches.
        actual = hashlib.sha256(payload).hexdigest()
        if pointer.sha256 and actual != pointer.sha256:
            raise ValueError(
                f"{pointer.key} no longer matches the digest recorded when it was stored. "
                "It has been modified or replaced since."
            )

        return io.BytesIO(payload)

    def delete(self, pointer: StoragePointer) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=self._full_key(pointer.key))

    def exists(self, pointer: StoragePointer) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._full_key(pointer.key))
        except ClientError as exc:
            error: dict[str, Any] = exc.response.get("Error", {})
            if error.get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True
