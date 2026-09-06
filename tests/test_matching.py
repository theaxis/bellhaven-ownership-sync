import unittest

from bellhaven_sync import Facility, address_score, apply_proposal, build_proposals, normalize_name, normalize_street, survivor_rank


class MatchingTests(unittest.TestCase):
    def test_street_normalization(self):
        self.assertEqual(normalize_street("4850 Northwest Sylvania Avenue"), normalize_street("4850 NW Sylvania Ave"))

    def test_name_normalization(self):
        self.assertEqual(normalize_name("Bellhaven Health Care Center"), normalize_name("Bellhaven Healthcare Center"))

    def test_address_dominates_renamed_facility(self):
        facility = Facility("x", "u", "New Name", "4930 West Lake Road", "Erie", "PA", "16505", ["Assisted Living"])
        account = {"name": "Old Name", "billing_street": "4930 W Lake Rd", "billing_city": "Erie", "billing_state": "PA", "billing_zip": "16505"}
        score, evidence = address_score(facility, account)
        self.assertGreaterEqual(score, .85)
        self.assertIn("ZIP exact", evidence)

    def test_pike_abbreviation(self):
        self.assertEqual(normalize_street("3313 Wilmington Pike"), normalize_street("3313 Wilmington Pk"))

    def test_current_parent_wins_duplicate_cluster(self):
        facility = Facility("x", "u", "Bellhaven Shores", "1 Lake Rd", "Erie", "PA", "16505", ["Assisted Living"])
        current = {"name": "Bellhaven Shores", "parent_id": "bellhaven", "status": "Active", "care_type": "Assisted Living", "billing_street": "1 Lake Rd", "lifetime_revenue": 0, "account_id": "1"}
        old = {"name": "Old Shores", "parent_id": "old", "status": "Active", "care_type": "Assisted Living", "billing_street": "1 Lake Road", "lifetime_revenue": 0, "account_id": "2"}
        self.assertGreater(survivor_rank(facility, current, "bellhaven"), survivor_rank(facility, old, "bellhaven"))

    def test_completed_chow_is_not_reproposed(self):
        facility = Facility("tiffin", "u", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", "OH", "44883", ["Short-Term Rehabilitation & Nursing"])
        parent = {"account_id": "bellhaven", "name": "Bellhaven Senior Living (Parent Account)"}
        current = {"account_id": "current", "name": facility.name, "parent_id": "bellhaven", "billing_street": facility.street, "billing_city": facility.city, "billing_state": facility.state, "billing_zip": facility.zip, "care_type": "Skilled Nursing", "status": "Active", "lifetime_revenue": 0, "outstanding_ar": 0, "duplicate_of_account": ""}
        old = {"account_id": "old", "name": facility.name, "parent_id": "prior", "billing_street": facility.street, "billing_city": facility.city, "billing_state": facility.state, "billing_zip": facility.zip, "care_type": "Skilled Nursing", "status": "Active", "lifetime_revenue": 100, "outstanding_ar": 10, "chow_current_account": "current", "duplicate_of_account": ""}
        proposals, _ = build_proposals([facility], [parent, current, old])
        self.assertEqual(proposals, [])

    def test_new_chow_changes_only_link_on_old_account(self):
        facility = Facility("tiffin", "u", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", "OH", "44883", ["Short-Term Rehabilitation & Nursing"])
        parent = {"account_id": "bellhaven", "name": "Bellhaven Senior Living (Parent Account)"}
        old = {"account_id": "old", "name": facility.name, "parent_id": "prior", "billing_street": facility.street, "billing_city": facility.city, "billing_state": facility.state, "billing_zip": facility.zip, "care_type": "Skilled Nursing", "status": "Active", "lifetime_revenue": 100, "outstanding_ar": 10, "chow_current_account": "", "duplicate_of_account": "", "note": "keep me"}
        proposals, _ = build_proposals([facility], [parent, old])
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["kind"], "chow")
        self.assertEqual(proposals[0]["payload"]["old_patch"], {"chow_current_account": "$NEW_ACCOUNT_ID"})

    def test_applied_duplicate_is_a_state_based_noop(self):
        facility = Facility("one", "u", "Bellhaven One", "1 Main St", "Town", "OH", "43000", ["Assisted Living"])
        parent = {"account_id": "p", "name": "Bellhaven Senior Living (Parent Account)"}
        current = {"account_id": "current", "name": facility.name, "parent_id": "p", "billing_street": facility.street, "billing_city": facility.city, "billing_state": facility.state, "billing_zip": facility.zip, "care_type": "Assisted Living", "status": "Active", "lifetime_revenue": 0, "outstanding_ar": 0, "duplicate_of_account": ""}
        note = "Duplicate of current account current; same normalized address and ZIP."
        losing = {"account_id": "losing", "name": "Old Name", "parent_id": "old", "billing_street": facility.street, "billing_city": facility.city, "billing_state": facility.state, "billing_zip": facility.zip, "care_type": "Assisted Living", "status": "Inactive", "lifetime_revenue": 0, "outstanding_ar": 0, "duplicate_of_account": "current", "chow_current_account": "", "note": note}
        proposals, _ = build_proposals([facility], [parent, current, losing])
        self.assertEqual(proposals, [])

    def test_applied_stale_flag_is_a_state_based_noop(self):
        parent = {"account_id": "p", "name": "Bellhaven Senior Living (Parent Account)"}
        note = "Ownership review: account is linked to Bellhaven but no current Bellhaven website location was found. Confirm sale/closure before removing parent relationship."
        stale = {"account_id": "stale", "name": "Old Bellhaven", "parent_id": "p", "status": "Needs Review", "billing_street": "9 Old Rd", "billing_zip": "43009", "duplicate_of_account": "", "chow_current_account": "", "note": note}
        proposals, _ = build_proposals([], [parent, stale])
        self.assertEqual(proposals, [])

    def test_absent_bellhaven_account_links_to_existing_cross_parent_chow(self):
        parent = {"account_id": "p", "name": "Bellhaven Senior Living (Parent Account)"}
        old = {"account_id": "old", "name": "Bellhaven of Sandusky", "parent_id": "p", "status": "Active", "billing_street": "2715 Columbus Ave", "billing_zip": "44870", "lifetime_revenue": 100, "outstanding_ar": 10, "duplicate_of_account": "", "chow_current_account": "", "note": ""}
        current = {"account_id": "current", "name": "Millstone Care of Sandusky", "parent_id": "millstone", "status": "Active", "billing_street": "2715 Columbus Avenue", "billing_zip": "44870", "lifetime_revenue": 0, "outstanding_ar": 0, "duplicate_of_account": "", "chow_current_account": "", "note": ""}
        proposals, _ = build_proposals([], [parent, old, current])
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["kind"], "chow_link_existing")
        self.assertEqual(proposals[0]["payload"], {"chow_current_account": "current"})

    def test_stale_approval_is_blocked(self):
        class FakeClient:
            def account(self, _):
                return {"account_id": "a", "name": "Changed elsewhere", "updated_at": "new"}
            def patch_account(self, *_):
                raise AssertionError("stale proposal must not write")

        proposal = {
            "kind": "update", "account_id": "a", "payload": {"name": "Proposed"},
            "account_before": {"name": "Old", "updated_at": "old"},
        }
        with self.assertRaisesRegex(RuntimeError, "changed after the proposal"):
            apply_proposal(FakeClient(), proposal)

    def test_already_applied_payload_is_safe_after_version_change(self):
        class FakeClient:
            def account(self, _):
                return {"account_id": "a", "name": "Proposed", "updated_at": "new"}
            def patch_account(self, *_):
                raise AssertionError("already-applied proposal must not write")

        proposal = {
            "kind": "update", "account_id": "a", "payload": {"name": "Proposed"},
            "account_before": {"name": "Old", "updated_at": "old"},
        }
        result = apply_proposal(FakeClient(), proposal)
        self.assertEqual(result["idempotent_reuse"]["name"], "Proposed")


if __name__ == "__main__":
    unittest.main()
