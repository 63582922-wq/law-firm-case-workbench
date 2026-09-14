"""Bounded native S3/SSE probe, project artifacts only; never a service installer."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import time
import urllib.request
from uuid import UUID

import boto3
from botocore.config import Config

parser = argparse.ArgumentParser()
parser.add_argument("root", type=Path)
mode = parser.add_mutually_exclusive_group()
mode.add_argument("--resume-run")
mode.add_argument("--inspect-run")
parser.add_argument("--pg-socket", type=Path)
parser.add_argument("--pg-data", type=Path)
args = parser.parse_args()
inspect_only = bool(args.inspect_run)
args.resume_run = args.resume_run or args.inspect_run
if args.resume_run:
    args.resume_run = str(UUID(args.resume_run))
    if not args.pg_socket or not args.pg_data:
        parser.error("resume requires explicit isolated PostgreSQL socket and data directory")
project = Path(__file__).resolve().parents[3]
root = args.root.resolve(strict=True)
if not root.is_relative_to(project / "artifacts") or root.stat().st_mode & 0o077:
    raise SystemExit("private project artifacts directory required")
if shutil.disk_usage(root).free < 1536 * 1024**2:
    raise SystemExit("insufficient disk headroom for bounded object-store probe")
binary = root / "minio"
release = json.loads((root / "release.json").read_text())
asset = next(x for x in release["assets"] if x["name"] == "minio.darwin-arm64.RELEASE.2025-09-07T16-13-09Z")
with binary.open("rb") as stream:
    digest = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
if asset.get("digest") != digest:
    raise SystemExit("official release digest mismatch")
binary.chmod(0o700)
for port in (19090, 19091):
    with socket.socket() as check:
        check.bind(("127.0.0.1", port))
credentials_file = root / "probe-credentials.json"
if args.resume_run:
    if credentials_file.is_symlink() or credentials_file.stat().st_mode & 0o077 or not (root / "data").is_dir():
        raise SystemExit("retained private storage is unavailable")
    credentials = json.loads(credentials_file.read_text())
elif credentials_file.exists() or (root / "data").exists():
    raise SystemExit("probe refuses to overwrite or reset an earlier store")
else:
    credentials = dict(access_key="native-probe-" + secrets.token_hex(8),
        secret_key=secrets.token_hex(32), kms="native-probe:" + base64.b64encode(secrets.token_bytes(32)).decode())
    with os.fdopen(os.open(credentials_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(credentials, stream)
        stream.flush()
        os.fsync(stream.fileno())
env = dict(os.environ, MINIO_ROOT_USER=credentials["access_key"], MINIO_ROOT_PASSWORD=credentials["secret_key"],
    MINIO_KMS_SECRET_KEY=credentials["kms"], MINIO_BROWSER="off", GOMEMLIMIT="96MiB", GOMAXPROCS="1")
endpoint = "http://127.0.0.1:19090"
health_client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
peak_rss = 0
log_name = (f"inspect-{args.resume_run}-{time.time_ns()}.log" if inspect_only else
    f"run-{args.resume_run}.log" if args.resume_run else "server.log")
with (root / log_name).open("xb") as log:
    process = subprocess.Popen([str(binary), "server", str(root / "data"), "--address", "127.0.0.1:19090",
        "--console-address", "127.0.0.1:19091", "--quiet"], env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 20
        while True:
            if process.poll() is not None:
                raise RuntimeError("native object store exited during startup; retained log")
            rss = int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(process.pid)], text=True).strip())
            peak_rss = max(peak_rss, rss)
            if rss > 256 * 1024:
                raise RuntimeError("native object store exceeds 256MiB startup RSS ceiling")
            try:
                with health_client.open(endpoint + "/minio/health/live", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("bounded object store startup timed out")
            time.sleep(0.2)
        client = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1",
            aws_access_key_id=credentials["access_key"], aws_secret_access_key=credentials["secret_key"],
            config=Config(connect_timeout=2, read_timeout=5, proxies={}, retries={"total_max_attempts": 1},
                          s3={"addressing_style": "path"}))
        bucket = "lawcase-native-synthetic"
        if args.resume_run:
            assert client.get_bucket_versioning(Bucket=bucket).get("Status") == "Enabled"
            from native_task import run_retained_task
            run_retained_task(run_id=args.resume_run, socket_path=args.pg_socket,
                data_path=args.pg_data, client=client, credentials=credentials, inspect_only=inspect_only)
            raise SystemExit(0)
        client.create_bucket(Bucket=bucket)
        client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        payload = b'{"synthetic":true}'
        written = client.put_object(Bucket=bucket, Key="probe.json", Body=payload, ServerSideEncryption="AES256")
        assert written.get("VersionId") and written.get("ServerSideEncryption") == "AES256"
        read = client.get_object(Bucket=bucket, Key="probe.json", VersionId=written["VersionId"])
        with read["Body"] as body:
            assert body.read() == payload
        rss = int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(process.pid)], text=True).strip())
        peak_rss = max(peak_rss, rss)
        if peak_rss > 256 * 1024:
            raise RuntimeError("native object store exceeds probe RSS ceiling")
        print(json.dumps(dict(status="VERSIONED_SSE_ROUNDTRIP_PASSED", observed_peak_rss_kib=peak_rss)))
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        print("Native object store stopped; data, credentials and log retained.")
