from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid4

from case_kernel.local_access_grants import LocalSessionProof
from case_kernel.local_intake_authorizations import (
    LocalEvidenceIntakeAuthorizationBlocked,
    LocalEvidenceIntakeAuthorizationRegistry,
)
from case_kernel.models import Actor, Role


class LocalEvidenceIntakeAuthorizationRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = LocalEvidenceIntakeAuthorizationRegistry()
        self.actor = Actor(
            actor_id=str(uuid4()),
            firm_id=str(uuid4()),
            roles=frozenset({Role.LEAD_LAWYER}),
        )
        self.session = LocalSessionProof(
            session_id=str(uuid4()),
            authentication_method="OS_BOUND_LOCAL_SESSION",
            authenticated_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

    def test_run_authorization_is_ephemeral_and_advances_only_its_version(self) -> None:
        run_id = str(uuid4())
        bound = self.registry.bind(
            run_id=run_id,
            matter_id=str(uuid4()),
            folder_grant_id=str(uuid4()),
            grant_actor=self.actor,
            grant_session=self.session,
            expected_version=3,
        )
        self.assertEqual(bound, self.registry.next_authorized_run())
        updated = self.registry.update_expected_version(run_id=run_id, expected_version=4)
        self.assertEqual(4, updated.expected_version)
        self.registry.remove(run_id=run_id)
        self.assertIsNone(self.registry.next_authorized_run())

    def test_system_worker_cannot_authorize_its_own_original_file_access(self) -> None:
        worker = Actor(self.actor.actor_id, self.actor.firm_id, frozenset({Role.SYSTEM_WORKER}))
        with self.assertRaises(LocalEvidenceIntakeAuthorizationBlocked):
            self.registry.bind(
                run_id=str(uuid4()),
                matter_id=str(uuid4()),
                folder_grant_id=str(uuid4()),
                grant_actor=worker,
                grant_session=self.session,
                expected_version=1,
            )

    def test_deferred_lease_is_not_reissued_until_the_durable_lease_expires(self) -> None:
        run_id = str(uuid4())
        bound = self.registry.bind(
            run_id=run_id,
            matter_id=str(uuid4()),
            folder_grant_id=str(uuid4()),
            grant_actor=self.actor,
            grant_session=self.session,
            expected_version=2,
        )
        retry_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        deferred = self.registry.defer_until(run_id=run_id, retry_not_before=retry_at)
        self.assertEqual(2, deferred.expected_version)
        self.assertIsNone(self.registry.next_authorized_run(now=retry_at - timedelta(seconds=1)))
        self.assertEqual(bound.run_id, self.registry.next_authorized_run(now=retry_at).run_id)


if __name__ == "__main__":
    unittest.main()
