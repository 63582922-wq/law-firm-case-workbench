from hashlib import sha256
import unittest

from case_kernel.evidence_refs import EvidenceLink
from case_kernel.models import Actor, Role
from case_kernel.party_identity_ledger import (
    IdentifierKind,
    LitigationRole,
    PartyIdentityBlocked,
    PartyIdentityLedger,
    RepresentationScope,
)


def evidence_link(*, label: str) -> EvidenceLink:
    return EvidenceLink(
        evidence_id=f"evidence_{label}",
        original_file_sha256=sha256(label.encode("utf-8")).hexdigest(),
        page_number=3,
        region_id=f"region_{label}",
        original_label=f"合成身份材料：{label}",
    )


class PartyIdentityLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assistant = Actor("alpha_assistant", "alpha_firm_001", frozenset({Role.ASSISTANT}))
        self.lead = Actor("alpha_lead", "alpha_firm_001", frozenset({Role.LEAD_LAWYER}))
        self.ledger = PartyIdentityLedger()

    def _party(self, *, name: str, role: LitigationRole):
        return self.ledger.add_party_candidate(
            self.assistant,
            display_name=name,
            role=role,
            evidence_links=(evidence_link(label=name),),
        )

    def test_identifier_requires_its_party_to_be_confirmed(self) -> None:
        party = self._party(name="合成原告", role=LitigationRole.PLAINTIFF)
        identifier = self.ledger.add_identifier_candidate(
            self.assistant,
            party_id=party.party_id,
            kind=IdentifierKind.WECHAT_NICKNAME,
            original_value="合成微信名",
            evidence_links=(evidence_link(label="nickname"),),
        )

        with self.assertRaisesRegex(PartyIdentityBlocked, "confirmed party"):
            self.ledger.confirm_identifier(self.lead, identifier_id=identifier.identifier_id, confirmation_hash="identifier-approval")

    def test_one_confirmed_identifier_cannot_point_to_two_confirmed_parties(self) -> None:
        plaintiff = self._party(name="合成原告", role=LitigationRole.PLAINTIFF)
        defendant = self._party(name="合成被告", role=LitigationRole.DEFENDANT)
        self.ledger.confirm_party(self.lead, party_id=plaintiff.party_id, confirmation_hash="plaintiff-approved")
        self.ledger.confirm_party(self.lead, party_id=defendant.party_id, confirmation_hash="defendant-approved")
        first = self.ledger.add_identifier_candidate(
            self.assistant,
            party_id=plaintiff.party_id,
            kind=IdentifierKind.WECHAT_NICKNAME,
            original_value="同一合成昵称",
            evidence_links=(evidence_link(label="first-nickname"),),
        )
        self.ledger.confirm_identifier(self.lead, identifier_id=first.identifier_id, confirmation_hash="first-approved")
        second = self.ledger.add_identifier_candidate(
            self.assistant,
            party_id=defendant.party_id,
            kind=IdentifierKind.WECHAT_NICKNAME,
            original_value="同一 合成昵称",
            evidence_links=(evidence_link(label="second-nickname"),),
        )

        with self.assertRaisesRegex(PartyIdentityBlocked, "conflicts"):
            self.ledger.confirm_identifier(self.lead, identifier_id=second.identifier_id, confirmation_hash="second-approved")

    def test_confirmed_representation_and_identifiers_enter_the_formal_snapshot(self) -> None:
        defendant = self._party(name="合成被告", role=LitigationRole.DEFENDANT)
        representative = self._party(name="合成代理人", role=LitigationRole.REPRESENTATIVE)
        self.ledger.confirm_party(self.lead, party_id=defendant.party_id, confirmation_hash="defendant-approved")
        self.ledger.confirm_party(self.lead, party_id=representative.party_id, confirmation_hash="representative-approved")
        phone = self.ledger.add_identifier_candidate(
            self.assistant,
            party_id=representative.party_id,
            kind=IdentifierKind.PHONE,
            original_value="180-0000-0000",
            evidence_links=(evidence_link(label="phone"),),
        )
        self.ledger.confirm_identifier(self.lead, identifier_id=phone.identifier_id, confirmation_hash="phone-approved")
        relationship = self.ledger.add_representation_candidate(
            self.assistant,
            represented_party_id=defendant.party_id,
            representative_party_id=representative.party_id,
            scope=RepresentationScope.FULL_AUTHORITY,
            evidence_links=(evidence_link(label="authorization"),),
        )
        self.ledger.confirm_representation(self.lead, representation_id=relationship.representation_id, confirmation_hash="representation-approved")

        snapshot = self.ledger.build_formal_snapshot(self.lead)

        self.assertEqual(len(snapshot.parties), 2)
        self.assertEqual(snapshot.identifiers[0].kind, IdentifierKind.PHONE)
        self.assertEqual(snapshot.representations[0].scope, RepresentationScope.FULL_AUTHORITY)

    def test_invalidating_a_party_revokes_its_identifier_and_representation(self) -> None:
        defendant = self._party(name="合成被告", role=LitigationRole.DEFENDANT)
        representative = self._party(name="合成代理人", role=LitigationRole.REPRESENTATIVE)
        self.ledger.confirm_party(self.lead, party_id=defendant.party_id, confirmation_hash="defendant-approved")
        self.ledger.confirm_party(self.lead, party_id=representative.party_id, confirmation_hash="representative-approved")
        identifier = self.ledger.add_identifier_candidate(
            self.assistant,
            party_id=defendant.party_id,
            kind=IdentifierKind.WECHAT_ACCOUNT,
            original_value="alpha_wechat_account",
            evidence_links=(evidence_link(label="wechat-account"),),
        )
        self.ledger.confirm_identifier(self.lead, identifier_id=identifier.identifier_id, confirmation_hash="identifier-approved")
        relationship = self.ledger.add_representation_candidate(
            self.assistant,
            represented_party_id=defendant.party_id,
            representative_party_id=representative.party_id,
            scope=RepresentationScope.PARTIAL_AUTHORITY,
            evidence_links=(evidence_link(label="representation"),),
        )
        self.ledger.confirm_representation(self.lead, representation_id=relationship.representation_id, confirmation_hash="representation-approved")
        self.ledger.invalidate_party(self.lead, party_id=defendant.party_id, reason_hash="party-corrected")

        snapshot = self.ledger.build_formal_snapshot(self.lead)
        self.assertEqual(len(snapshot.parties), 1)
        self.assertEqual(snapshot.identifiers, ())
        self.assertEqual(snapshot.representations, ())


if __name__ == "__main__":
    unittest.main()
