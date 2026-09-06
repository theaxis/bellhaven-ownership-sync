"""Health checks for the daily Bellhaven reconciliation.

The monitor is intentionally deterministic. A non-zero exit makes the scheduled
GitHub Actions job fail visibly instead of allowing an empty or partial scrape to
look like a successful ownership update.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from bellhaven_sync import (
    ApiClient,
    DEFAULT_BASE_URL,
    normalize_name,
    normalize_street,
    same_physical_address,
    scrape_facilities,
)


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "monitoring.json"
REPORT_PATH = ROOT / "data" / "health_report.json"


def directory_declared_count(base_url: str) -> int | None:
    response = requests.get(f"{base_url}/communities", timeout=20)
    response.raise_for_status()
    match = re.search(r"(\d+)\s+communities listed", response.text)
    return int(match.group(1)) if match else None


def health_report(
    facilities: list,
    accounts: list[dict[str, Any]],
    declared_count: int | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    failures: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    def fail(check: str, detail: str) -> None:
        failures.append({"check": check, "detail": detail})

    def warn(check: str, detail: str) -> None:
        warnings.append({"check": check, "detail": detail})

    count = len(facilities)
    expected = config["expected_directory_count"]
    if declared_count is None:
        fail("directory_count_label", "Could not find the website's declared community count; page structure may have changed")
    elif declared_count != count:
        fail("scrape_completeness", f"Directory declares {declared_count} locations but scraper returned {count}")
    if count != expected:
        warn("portfolio_count_change", f"Expected baseline {expected}; current directory contains {count}. Review and update the baseline after confirming the change")
    if not config["minimum_directory_count"] <= count <= config["maximum_directory_count"]:
        fail("portfolio_count_bounds", f"Scraped count {count} is outside configured bounds")

    required = ("slug", "name", "street", "city", "state", "zip", "care_offerings")
    for facility in facilities:
        missing = [field for field in required if not getattr(facility, field)]
        if missing:
            fail("required_source_fields", f"{facility.slug or '<missing slug>'}: missing {', '.join(missing)}")

    slugs = [f.slug for f in facilities]
    if len(set(slugs)) != len(slugs):
        fail("unique_source_slugs", "Duplicate facility slugs were scraped")
    source_addresses = [(normalize_street(f.street), f.zip) for f in facilities]
    if len(set(source_addresses)) != len(source_addresses):
        fail("unique_source_addresses", "Multiple website locations share a normalized street and ZIP")

    parents = [a for a in accounts if normalize_name(a.get("name", "")) == normalize_name("Bellhaven Senior Living (Parent Account)")]
    if len(parents) != 1:
        fail("single_bellhaven_parent", f"Expected one Bellhaven parent account; found {len(parents)}")
        parent_id = None
    else:
        parent_id = parents[0]["account_id"]

    ids = {a["account_id"] for a in accounts}
    orphans = [a["account_id"] for a in accounts if a.get("parent_id") and a["parent_id"] not in ids]
    if orphans:
        fail("parent_referential_integrity", f"{len(orphans)} accounts reference a missing parent")

    missing_current: list[str] = []
    if parent_id:
        current_children = [a for a in accounts if a.get("parent_id") == parent_id and a.get("status") == "Active"]
        for facility in facilities:
            if not any(same_physical_address(facility, account) for account in current_children):
                missing_current.append(facility.name)
        if missing_current:
            warn("current_portfolio_coverage", f"{len(missing_current)} published facilities lack an active account under Bellhaven: {', '.join(missing_current)}")

    broken_duplicate_links = [a["account_id"] for a in accounts if a.get("duplicate_of_account") and a["duplicate_of_account"] not in ids]
    if broken_duplicate_links:
        fail("duplicate_referential_integrity", f"{len(broken_duplicate_links)} duplicate links point to missing accounts")
    broken_chow_links = [a["account_id"] for a in accounts if a.get("chow_current_account") and a["chow_current_account"] not in ids]
    if broken_chow_links:
        fail("chow_referential_integrity", f"{len(broken_chow_links)} CHOW links point to missing accounts")

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "failed" if failures else ("attention" if warnings else "healthy"),
        "metrics": {
            "scraped_facilities": count,
            "declared_facilities": declared_count,
            "crm_accounts": len(accounts),
            "active_bellhaven_coverage": count - len(missing_current),
        },
        "failures": failures,
        "warnings": warnings,
    }


def main() -> int:
    config = json.loads(CONFIG_PATH.read_text())
    client = ApiClient()
    facilities = scrape_facilities(client.base_url)
    report = health_report(facilities, client.accounts(), directory_declared_count(client.base_url), config)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    # Both structural failures and meaningful portfolio changes should make the
    # scheduled job visible to an operator.
    return 0 if report["status"] == "healthy" else 1


if __name__ == "__main__":
    sys.exit(main())
