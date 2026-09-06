import unittest

from bellhaven_sync import Facility
from monitor import health_report


CONFIG = {"expected_directory_count": 1, "minimum_directory_count": 1, "maximum_directory_count": 5}


class MonitorTests(unittest.TestCase):
    def records(self):
        facility = Facility("one", "u", "Bellhaven One", "1 Main St", "Town", "OH", "43000", ["Assisted Living"])
        parent = {"account_id": "p", "name": "Bellhaven Senior Living (Parent Account)", "parent_id": ""}
        child = {"account_id": "c", "name": facility.name, "parent_id": "p", "status": "Active", "billing_street": facility.street, "billing_zip": facility.zip, "duplicate_of_account": "", "chow_current_account": ""}
        return facility, parent, child

    def test_healthy_state(self):
        facility, parent, child = self.records()
        report = health_report([facility], [parent, child], 1, CONFIG)
        self.assertEqual(report["status"], "healthy")

    def test_partial_scrape_fails(self):
        facility, parent, child = self.records()
        report = health_report([facility], [parent, child], 2, CONFIG)
        self.assertEqual(report["status"], "failed")
        self.assertIn("scrape_completeness", [x["check"] for x in report["failures"]])

    def test_missing_current_account_alerts(self):
        facility, parent, _ = self.records()
        report = health_report([facility], [parent], 1, CONFIG)
        self.assertEqual(report["status"], "attention")
        self.assertIn("current_portfolio_coverage", [x["check"] for x in report["warnings"]])


if __name__ == "__main__":
    unittest.main()
