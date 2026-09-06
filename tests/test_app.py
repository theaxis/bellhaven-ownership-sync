import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import decision_fields, outcome_summary, page, persistent_flag_reason, proposal_category, readable_timestamp, render_diff, website_absent_disposition


class ReviewAppTests(unittest.TestCase):
    def test_proposal_categories_cover_assignment_classes(self):
        self.assertEqual(proposal_category({"kind": "create"}), "Missing CRM accounts")
        self.assertEqual(proposal_category({"kind": "update"}), "Matched accounts needing correction")
        self.assertEqual(proposal_category({"kind": "chow"}), "Ownership changes")
        self.assertEqual(proposal_category({"kind": "duplicate"}), "Duplicate records")
        self.assertEqual(proposal_category({"kind": "stale"}), "Bellhaven accounts absent from the website")

    def test_regular_diff_shows_current_and_proposed_values(self):
        rendered = render_diff({
            "kind": "update",
            "account_before": {"name": "Old Name"},
            "payload": {"name": "New Name"},
        })
        self.assertIn("Old Name", rendered)
        self.assertIn("New Name", rendered)

    def test_chow_diff_separates_new_and_historical_records(self):
        rendered = render_diff({
            "kind": "chow",
            "account_before": {"chow_current_account": ""},
            "payload": {
                "create": {"name": "Current Facility"},
                "old_patch": {"chow_current_account": "$NEW_ACCOUNT_ID"},
            },
        })
        self.assertIn("New current account", rendered)
        self.assertIn("Historical account", rendered)
        self.assertIn("$NEW_ACCOUNT_ID", rendered)

    def test_legacy_history_describes_saved_update_fields(self):
        action, title, account_id = outcome_summary({
            "result": {
                "account_id": "001",
                "message": "updated",
                "fields": ["duplicate_of_account", "note", "status"],
            }
        }, {"001": "Old Facility"})
        self.assertEqual(action, "duplicate")
        self.assertEqual(title, "Marked Old Facility as a duplicate and inactive")
        self.assertEqual(account_id, "001")

    def test_history_timestamp_omits_seconds_and_microseconds(self):
        self.assertEqual(
            readable_timestamp("2026-09-05T20:31:56.688350+00:00"),
            "Sep 5, 2026 · 20:31 UTC",
        )

    def test_review_queue_excludes_decided_findings(self):
        with tempfile.TemporaryDirectory() as folder:
            proposals_path = Path(folder) / "proposals.json"
            decisions_path = Path(folder) / "decisions.json"
            proposals_path.write_text(json.dumps({
                "summary": {"website_facilities": 2},
                "proposals": [
                    {"id": "pending", "title": "Pending proposal", "kind": "update", "confidence": "high", "payload": {}, "decision": {"status": "pending"}},
                    {"id": "rejected", "title": "Rejected proposal", "kind": "update", "confidence": "high", "payload": {}, "decision": {"status": "rejected"}},
                ],
            }))
            decisions_path.write_text(json.dumps({"rejected": {"status": "rejected"}}))
            account_index_path = Path(folder) / "account_index.json"
            account_index_path.write_text("{}")
            with patch("app.PROPOSALS_PATH", proposals_path), patch("app.DECISIONS_PATH", decisions_path), patch("app.ACCOUNT_INDEX_PATH", account_index_path):
                rendered = page(view="pending")
        self.assertIn("Pending proposal", rendered)
        self.assertNotIn("Rejected proposal", rendered)

    def test_summary_counts_link_to_detail_views(self):
        with tempfile.TemporaryDirectory() as folder:
            proposals_path = Path(folder) / "proposals.json"
            decisions_path = Path(folder) / "decisions.json"
            account_index_path = Path(folder) / "account_index.json"
            account_snapshot_path = Path(folder) / "account_snapshot.json"
            proposals_path.write_text(json.dumps({
                "summary": {
                    "website_facilities": 1,
                    "classification_counts": {"confident_match": 1},
                    "matches": [{"facility": "Bellhaven One", "source_url": "https://example.test/one", "account": "Bellhaven One", "account_id": "one", "result": "confident_match", "score": 1.0}],
                },
                "proposals": [],
            }))
            decisions_path.write_text(json.dumps({
                "approved": {"status": "approved", "decided_at": "2026-09-05T20:00:00+00:00", "result": {"account_id": "one", "message": "updated", "fields": ["name"]}},
                "approved-earlier": {"status": "approved", "decided_at": "2026-09-05T17:00:00+00:00", "result": {"account_id": "two", "message": "created"}},
            }))
            account_index_path.write_text(json.dumps({"one": "Bellhaven One", "two": "Bellhaven Two"}))
            account_snapshot_path.write_text(json.dumps({"one": {"account_id": "one", "name": "Bellhaven One", "status": "Active"}}))
            with patch("app.PROPOSALS_PATH", proposals_path), patch("app.DECISIONS_PATH", decisions_path), patch("app.ACCOUNT_INDEX_PATH", account_index_path), patch("app.ACCOUNT_SNAPSHOT_PATH", account_snapshot_path):
                queue = page()
                pending = page(view="pending")
                facilities = page(view="facilities")
                approved = page(view="history", decision_filter="approved")
                approved_oldest = page(view="history", decision_filter="approved", sort_order="oldest")
                rejected = page(view="history", decision_filter="rejected")
                account = page(view="account", account_id="one")
        self.assertIn('href="/?view=facilities"', queue)
        self.assertIn('href="/?view=history&amp;status=approved"', queue)
        self.assertIn("Latest run", queue)
        self.assertIn('href="/?view=pending"', queue)
        self.assertIn(">Home</a>", queue)
        self.assertNotIn("Pending decisions", queue)
        self.assertIn("Pending decisions", pending)
        self.assertIn("No pending decisions", pending)
        self.assertIn("Confident match", facilities)
        self.assertIn("Change made", approved)
        self.assertIn("Fields affected", approved)
        self.assertIn("Updated Bellhaven One", approved)
        self.assertLess(approved.index("Updated Bellhaven One"), approved.index("Created Bellhaven Two"))
        self.assertLess(approved_oldest.index("Created Bellhaven Two"), approved_oldest.index("Updated Bellhaven One"))
        self.assertIn("order=oldest", approved)
        self.assertIn("No rejected decisions", rejected)
        self.assertIn("Read-only CRM snapshot", account)
        self.assertIn("Updated Bellhaven One", account)

    def test_website_absent_records_remain_visible_after_disposition(self):
        with tempfile.TemporaryDirectory() as folder:
            proposals_path = Path(folder) / "proposals.json"
            decisions_path = Path(folder) / "decisions.json"
            account_index_path = Path(folder) / "account_index.json"
            account_snapshot_path = Path(folder) / "account_snapshot.json"
            proposals_path.write_text(json.dumps({
                "summary": {
                    "website_facilities": 1,
                    "bellhaven_parent_id": "parent",
                    "classification_counts": {"confident_match": 1, "absent_from_website": 0},
                    "matches": [{"facility": "Current", "account_id": "current", "result": "confident_match", "score": 1.0}],
                },
                "proposals": [],
            }))
            decisions_path.write_text("{}")
            account_index_path.write_text(json.dumps({"duplicate": "Survivor", "millstone": "Millstone Current"}))
            account_snapshot_path.write_text(json.dumps({
                "current": {"account_id": "current", "name": "Current", "parent_id": "parent", "status": "Active"},
                "review": {"account_id": "review", "name": "Needs Review Facility", "parent_id": "parent", "status": "Needs Review"},
                "old": {"account_id": "old", "name": "Historical Facility", "parent_id": "parent", "status": "Active", "chow_current_account": "millstone"},
            }))
            with patch("app.PROPOSALS_PATH", proposals_path), patch("app.DECISIONS_PATH", decisions_path), patch("app.ACCOUNT_INDEX_PATH", account_index_path), patch("app.ACCOUNT_SNAPSHOT_PATH", account_snapshot_path):
                queue = page()
                exceptions = page(view="exceptions")
                all_exceptions = page(view="exceptions", exception_filter="all")
                chow_exceptions = page(view="exceptions", exception_filter="chow")
        self.assertIn("1 flagged record", queue)
        self.assertIn("2 Bellhaven-linked CRM records are absent", queue)
        self.assertIn("Flagged records and exceptions", exceptions)
        self.assertIn("Needs Review", exceptions)
        self.assertIn("Open (1)", exceptions)
        self.assertIn("Resolved (1)", exceptions)
        self.assertIn("Website absent (2)", exceptions)
        self.assertIn("sort=status", exceptions)
        self.assertIn("Needs Review Facility", exceptions)
        self.assertNotIn("Historical CHOW record", exceptions)
        self.assertIn("Historical CHOW record", all_exceptions)
        self.assertIn("Millstone Current", chow_exceptions)
        self.assertNotIn("Needs Review Facility", chow_exceptions)

    def test_website_absent_dispositions_distinguish_review_duplicate_and_chow(self):
        names = {"winner": "Surviving Account", "current": "Current Owner Account"}
        self.assertEqual(website_absent_disposition({"status": "Needs Review"}, names)[0], "Needs ownership review")
        self.assertIn("Surviving Account", website_absent_disposition({"duplicate_of_account": "winner"}, names)[1])
        self.assertIn("Current Owner Account", website_absent_disposition({"chow_current_account": "current"}, names)[1])

    def test_persistent_flag_reason_supports_non_website_flags(self):
        self.assertIn("Absent from the website", persistent_flag_reason({"account_id": "absent"}, {"absent"}))
        self.assertEqual(persistent_flag_reason({"account_id": "current"}, {"absent"}), "The CRM status requires manual review.")

    def test_decision_fields_reads_saved_api_outcome(self):
        self.assertEqual(
            decision_fields({"result": {"fields": ["name", "parent_id"]}}),
            ["name", "parent_id"],
        )

    def test_earlier_review_flag_is_labeled_superseded_by_later_chow(self):
        with tempfile.TemporaryDirectory() as folder:
            proposals_path = Path(folder) / "proposals.json"
            decisions_path = Path(folder) / "decisions.json"
            account_index_path = Path(folder) / "account_index.json"
            proposals_path.write_text(json.dumps({"summary": {}, "proposals": []}))
            account_index_path.write_text(json.dumps({"old": "Bellhaven of Sandusky"}))
            decisions_path.write_text(json.dumps({
                "old-review": {"status": "approved", "decided_at": "2026-09-05T17:00:00+00:00", "result": {"account_id": "old", "message": "updated", "fields": ["note", "status"]}},
                "new-chow": {"status": "approved", "decided_at": "2026-09-05T20:00:00+00:00", "result": {"account_id": "old", "message": "updated", "fields": ["chow_current_account"]}},
            }))
            with patch("app.PROPOSALS_PATH", proposals_path), patch("app.DECISIONS_PATH", decisions_path), patch("app.ACCOUNT_INDEX_PATH", account_index_path):
                rendered = page(view="history")
        self.assertIn("Superseded", rendered)
        self.assertIn("Replaced by the later CHOW resolution", rendered)


if __name__ == "__main__":
    unittest.main()
