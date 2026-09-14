from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4
import unittest

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_api.web_derivative_delivery import WebDerivativeDeliveryBlocked, WebDerivativeDeliveryService
from case_kernel.models import Actor, Role


def _identity(*, firm_id: str | None = None) -> ServerIdentityContext:
    now = datetime.now(timezone.utc)
    return ServerIdentityContext(
        actor=Actor(actor_id=str(uuid4()), firm_id=firm_id or str(uuid4()), roles=frozenset({Role.LEAD_LAWYER})),
        session_id=str(uuid4()),
        issuer="https://id.example.test/oidc",
        authentication_method=AuthenticationMethod.OIDC_MFA,
        authenticated_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=30),
    )


class _Store:
    def __init__(self, locator: object) -> None:
        self.locator = locator

    def get_verified_derivative_locator(self, **kwargs):
        del kwargs
        return self.locator


class _ObjectStore:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def materialize_verified_derivative(self, **kwargs):
        target = Path(kwargs["destination"])
        target.write_bytes(self.content)
        return target


class WebDerivativeDeliveryTests(unittest.TestCase):
    def test_only_verified_derivative_is_returned_with_safe_filename(self) -> None:
        identity = _identity()
        matter_id = str(uuid4())
        derivative_id = str(uuid4())
        content = b"%PDF-1.7 verified derivative"
        artifact_hash = sha256(content).hexdigest()
        locator = SimpleNamespace(
            firm_id=identity.actor.firm_id,
            matter_id=matter_id,
            derivative_id=derivative_id,
            artifact_type="ANNOTATED_RELATED_PAGES_PDF",
            object_key=f"{artifact_hash[:2]}/{artifact_hash[2:4]}/{artifact_hash}.lca",
            artifact_sha256=artifact_hash,
            page_count=1,
            status="VERIFIED",
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            result = WebDerivativeDeliveryService(
                evidence_store=_Store(locator),
                object_store=_ObjectStore(content),
                worker_root=root,
            ).download(identity=identity, matter_id=matter_id, derivative_id=derivative_id)
        self.assertEqual(result.file_name, "相关页面（红框）.pdf")
        self.assertEqual(result.content, content)

    def test_cross_firm_locator_is_rejected_before_object_store(self) -> None:
        identity = _identity()
        locator = SimpleNamespace(
            firm_id=str(uuid4()),
            matter_id=str(uuid4()),
            status="VERIFIED",
            artifact_sha256="a" * 64,
            object_key="aa/aa/" + "a" * 64 + ".lca",
            page_count=1,
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            with self.assertRaises(WebDerivativeDeliveryBlocked):
                WebDerivativeDeliveryService(
                    evidence_store=_Store(locator),
                    object_store=_ObjectStore(b"not used"),
                    worker_root=root,
                ).download(identity=identity, matter_id=str(uuid4()), derivative_id=str(uuid4()))


if __name__ == "__main__":
    unittest.main()
