#!/usr/bin/env python3
"""Exercise the exact private S3 TLS + SSE-S3 contract for both principals."""

from __future__ import annotations

import base64
from collections.abc import Callable
from hashlib import sha256
import os
import sys

import boto3
from botocore import UNSIGNED
from botocore.client import Config
from botocore.exceptions import ClientError


class ObjectStoreProbeBlocked(RuntimeError):
    """Non-secret stage marker for a failed least-privilege assertion."""


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"missing probe setting: {name}")
    return value


def expect_access_denied(label: str, operation: Callable[[], object]) -> None:
    try:
        operation()
    except ClientError as error:
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = error.response.get("Error", {}).get("Code")
        if status not in {401, 403} or code not in {
            "AccessDenied",
            "AllAccessDisabled",
        }:
            raise ObjectStoreProbeBlocked(f"{label}_unexpected_response") from error
        return
    raise ObjectStoreProbeBlocked(f"{label}_unexpected_authority")


def verify_principal(label: str, access_key: str, secret_key: str) -> None:
    endpoint = required("LAWCASE_OBJECT_ENDPOINT")
    bucket = required("LAWCASE_OBJECT_BUCKET")
    region = required("LAWCASE_OBJECT_REGION")
    ca_bundle = required("LAWCASE_CA_BUNDLE")
    body = f"lawcase-local-managed-sse-probe:{label}".encode("ascii")
    checksum = base64.b64encode(sha256(body).digest()).decode("ascii")
    key = f"deployment-probes/v1/{label}-{sha256(body).hexdigest()}.bin"
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        verify=ca_bundle,
    )
    # MinIO derives ListBuckets visibility from bucket-level ListBucket.  The
    # principal may therefore see its one assigned bucket, but no others, and
    # it must not gain policy-administration visibility.
    visible_buckets = {
        item.get("Name")
        for item in client.list_buckets().get("Buckets", [])
        if isinstance(item, dict)
    }
    if visible_buckets != {bucket}:
        raise ObjectStoreProbeBlocked(f"{label}_bucket_visibility_differs")
    expect_access_denied(
        f"{label}_get_bucket_policy",
        lambda: client.get_bucket_policy(Bucket=bucket),
    )
    deleted = False
    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentLength=len(body),
            ChecksumAlgorithm="SHA256",
            ChecksumSHA256=checksum,
            ServerSideEncryption="AES256",
        )
        head = client.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
        if head.get("ServerSideEncryption") != "AES256":
            raise RuntimeError(f"{label} object was not stored with AES256 SSE-S3")
        if head.get("ChecksumSHA256") != checksum:
            raise RuntimeError(f"{label} object checksum metadata differs")
        response = client.get_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
        stream = response["Body"]
        try:
            if stream.read() != body:
                raise RuntimeError(f"{label} object round-trip integrity failed")
        finally:
            stream.close()
        if label == "web":
            anonymous = boto3.client(
                "s3",
                endpoint_url=endpoint,
                region_name=region,
                verify=ca_bundle,
                config=Config(signature_version=UNSIGNED),
            )
            expect_access_denied(
                "anonymous_get_object",
                lambda: anonymous.get_object(Bucket=bucket, Key=key),
            )
        client.delete_object(Bucket=bucket, Key=key)
        deleted = True
        try:
            client.head_object(Bucket=bucket, Key=key)
        except ClientError as error:
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = error.response.get("Error", {}).get("Code")
            if status != 404 and code not in {"404", "NoSuchKey", "NotFound"}:
                raise
        else:
            raise RuntimeError(f"{label} object remained visible after delete")
    except Exception:
        if not deleted:
            try:
                client.delete_object(Bucket=bucket, Key=key)
            except Exception:
                pass
        raise


def main() -> int:
    try:
        web_access = required("LAWCASE_WEB_OBJECT_ACCESS_KEY_ID")
        web_secret = required("LAWCASE_WEB_OBJECT_SECRET_ACCESS_KEY")
        worker_access = required("LAWCASE_WORKER_OBJECT_ACCESS_KEY_ID")
        worker_secret = required("LAWCASE_WORKER_OBJECT_SECRET_ACCESS_KEY")
        if web_access == worker_access or web_secret == worker_secret:
            raise RuntimeError("object-store principals are not independent")
        verify_principal(
            "web",
            web_access,
            web_secret,
        )
        verify_principal(
            "worker",
            worker_access,
            worker_secret,
        )
    except ObjectStoreProbeBlocked as error:
        print(
            f"private object-store contract failed: stage={error}",
            file=sys.stderr,
        )
        return 2
    except Exception as error:
        # SDK messages may include request identifiers or credential metadata;
        # the persistent probe log only needs the non-secret failure class.
        print(
            f"private object-store contract failed: {type(error).__name__}",
            file=sys.stderr,
        )
        return 2
    print(
        "private object-store TLS, scoped visibility, denied bucket administration/anonymous read, "
        "independent principals and AES256 SSE-S3: PASS"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
