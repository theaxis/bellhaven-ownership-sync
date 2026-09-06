"""Bellhaven facility ownership reconciliation.

The website is treated as the reference for Bellhaven's publicly listed
portfolio, while the CRM remains the system of record for account and billing
history. Discrepancies become reviewable proposals, and changes are applied only
after a human approves them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


DEFAULT_BASE_URL = "https://analyst-assessment-production.up.railway.app"
DATA_DIR = Path(__file__).resolve().parent / "data"
PROPOSALS_PATH = DATA_DIR / "proposals.json"
DECISIONS_PATH = DATA_DIR / "decisions.json"
ACCOUNT_INDEX_PATH = DATA_DIR / "account_index.json"
ACCOUNT_SNAPSHOT_PATH = DATA_DIR / "account_snapshot.json"

CARE_MAP = {
    "Assisted Living": "Assisted Living",
    "Memory Support": "Memory Care",
    "Short-Term Rehabilitation & Nursing": "Skilled Nursing",
}
STREET_WORDS = {
    "street": "st", "st.": "st", "road": "rd", "rd.": "rd",
    "avenue": "ave", "ave.": "ave", "boulevard": "blvd", "blvd.": "blvd",
    "drive": "dr", "dr.": "dr", "lane": "ln", "ln.": "ln",
    "highway": "hwy", "parkway": "pkwy", "circle": "cir", "pk": "pike",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
}


@dataclass
class Facility:
    slug: str
    url: str
    name: str
    street: str
    city: str
    state: str
    zip: str
    care_offerings: list[str]
    phone: str = ""


class ApiClient:
    def __init__(self, token: str | None = None, base_url: str | None = None):
        self.base_url = (base_url or os.getenv("BELLHAVEN_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        token = token or os.getenv("BELLHAVEN_API_TOKEN")
        if not token:
            raise RuntimeError("Set BELLHAVEN_API_TOKEN before accessing the CRM")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def accounts(self) -> list[dict[str, Any]]:
        response = self.session.get(f"{self.base_url}/api/v1/accounts", params={"page_size": 200}, timeout=20)
        response.raise_for_status()
        return response.json()["data"]

    def account(self, account_id: str) -> dict[str, Any]:
        response = self.session.get(f"{self.base_url}/api/v1/accounts/{account_id}", timeout=20)
        response.raise_for_status()
        return response.json()

    def patch_account(self, account_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        response = self.session.patch(f"{self.base_url}/api/v1/accounts/{account_id}", json=fields, timeout=20)
        response.raise_for_status()
        return response.json()

    def create_account(self, fields: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(f"{self.base_url}/api/v1/accounts", json=fields, timeout=20)
        response.raise_for_status()
        return response.json()


def clean_text(value: str) -> str:
    return " ".join(value.split())


def normalize_name(value: str) -> str:
    value = value.lower().replace("&", " and ")
    value = re.sub(r"\bhealth\s+care\b", "healthcare", value)
    value = re.sub(r"\brehab\b", "rehabilitation", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    stop = {"the"}
    return " ".join(part for part in value.split() if part not in stop)


def normalize_street(value: str) -> str:
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9 ]+", " ", value)
    return " ".join(STREET_WORDS.get(part, part) for part in value.split())


def scrape_facilities(base_url: str = DEFAULT_BASE_URL) -> list[Facility]:
    session = requests.Session()
    links: dict[str, str] = {}
    page = 1
    while True:
        response = session.get(f"{base_url}/communities", params={"page": page}, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for anchor in soup.select('.card h3 a[href^="/communities/"]'):
            links[anchor["href"].rstrip("/").split("/")[-1]] = urljoin(base_url, anchor["href"])
        next_link = soup.find("a", string=lambda text: bool(text and "Next" in text))
        if not next_link:
            break
        page += 1

    facilities: list[Facility] = []
    for slug, url in links.items():
        response = session.get(url, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        details: dict[str, Any] = {}
        for dt in soup.select("dl.detail dt"):
            dd = dt.find_next_sibling("dd")
            if dd:
                details[clean_text(dt.get_text(" "))] = dd
        address_lines = list(details["Address"].stripped_strings)
        match = re.match(r"(.+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$", address_lines[-1])
        if not match:
            raise ValueError(f"Could not parse address for {slug}: {address_lines}")
        offerings = [clean_text(x.get_text(" ")) for x in details["Care Offerings"].select(".badge")]
        facilities.append(Facility(
            slug=slug,
            url=url,
            name=clean_text(soup.select_one("h1").get_text(" ")),
            street=clean_text(" ".join(address_lines[:-1])),
            city=clean_text(match.group(1)),
            state=match.group(2),
            zip=match.group(3),
            care_offerings=offerings,
            phone=clean_text(details.get("Phone").get_text(" ")) if details.get("Phone") else "",
        ))
    return facilities


def address_score(facility: Facility, account: dict[str, Any]) -> tuple[float, list[str]]:
    evidence: list[str] = []
    score = 0.0
    f_street, a_street = normalize_street(facility.street), normalize_street(account.get("billing_street", ""))
    if facility.zip and facility.zip == account.get("billing_zip"):
        score += 0.35; evidence.append("ZIP exact")
    if normalize_name(facility.city) == normalize_name(account.get("billing_city", "")) and facility.state == account.get("billing_state"):
        score += 0.20; evidence.append("city/state exact")
    street_ratio = SequenceMatcher(None, f_street, a_street).ratio() if f_street and a_street else 0
    if street_ratio >= 0.98:
        score += 0.35; evidence.append("street exact after normalization")
    elif street_ratio >= 0.82:
        score += 0.28; evidence.append(f"street similar ({street_ratio:.0%})")
    name_ratio = SequenceMatcher(None, normalize_name(facility.name), normalize_name(account.get("name", ""))).ratio()
    if name_ratio >= 0.98:
        score += 0.25; evidence.append("name exact after normalization")
    elif name_ratio >= 0.72:
        score += 0.17; evidence.append(f"name similar ({name_ratio:.0%})")
    return min(score, 1.0), evidence


def proposal_id(kind: str, source_key: str, account_id: str, payload: dict[str, Any]) -> str:
    raw = json.dumps([kind, source_key, account_id, payload], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def make_proposal(kind: str, title: str, source_key: str, account: dict[str, Any] | None,
                  payload: dict[str, Any], evidence: list[str], confidence: str,
                  facility: Facility | None = None) -> dict[str, Any]:
    account_id = (account or {}).get("account_id", "")
    return {
        "id": proposal_id(kind, source_key, account_id, payload),
        "kind": kind,
        "title": title,
        "source_key": source_key,
        "account_id": account_id,
        "account_before": account or {},
        "payload": payload,
        "evidence": evidence,
        "confidence": confidence,
        "source": asdict(facility) if facility else {},
    }


def desired_fields(f: Facility, parent_id: str) -> dict[str, Any]:
    care = "; ".join(CARE_MAP.get(x, x) for x in f.care_offerings)
    return {
        "name": f.name,
        "parent_id": parent_id,
        "billing_street": f.street,
        "billing_city": f.city,
        "billing_state": f.state,
        "billing_zip": f.zip,
        "care_type": care,
        "status": "Active",
    }


def changed_fields(account: dict[str, Any], desired: dict[str, Any]) -> dict[str, Any]:
    changes = {}
    for key, value in desired.items():
        current = account.get(key, "")
        # Do not churn harmless postal abbreviations on otherwise identical streets.
        if key == "billing_street" and normalize_street(current) == normalize_street(value):
            continue
        if current != value:
            changes[key] = value
    return changes


def same_physical_address(facility: Facility, account: dict[str, Any]) -> bool:
    return bool(
        facility.zip == account.get("billing_zip")
        and normalize_street(facility.street) == normalize_street(account.get("billing_street", ""))
    )


def survivor_rank(facility: Facility, account: dict[str, Any], parent_id: str) -> tuple:
    desired_care = "; ".join(CARE_MAP.get(x, x) for x in facility.care_offerings)
    return (
        normalize_name(facility.name) == normalize_name(account.get("name", "")),
        account.get("parent_id") == parent_id,
        account.get("status") == "Active" and not account.get("duplicate_of_account"),
        account.get("care_type") == desired_care,
        account.get("lifetime_revenue", 0) > 0,
        clean_text(facility.street).lower() == clean_text(account.get("billing_street", "")).lower(),
        bool(account.get("parent_id")),
        normalize_street(facility.street) == normalize_street(account.get("billing_street", "")),
        account.get("account_id", ""),
    )


def build_proposals(facilities: list[Facility], accounts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    parents = [a for a in accounts if normalize_name(a["name"]) == normalize_name("Bellhaven Senior Living (Parent Account)")]
    if len(parents) != 1:
        raise ValueError(f"Expected one Bellhaven parent account; found {len(parents)}")
    parent = parents[0]
    parent_id = parent["account_id"]
    candidates = [a for a in accounts if "parent account" not in a["name"].lower()]
    proposals: list[dict[str, Any]] = []
    matched_ids: set[str] = set()
    handled_ids: set[str] = set()
    matches: list[dict[str, Any]] = []

    for facility in facilities:
        ranked = sorted(((*address_score(facility, a), a) for a in candidates), key=lambda x: x[0], reverse=True)
        if not ranked:
            payload = desired_fields(facility, parent_id)
            proposals.append(make_proposal(
                "create", f"Create missing CRM account: {facility.name}", facility.slug, None, payload,
                ["CRM contains no facility candidates"], "high", facility,
            ))
            matches.append({"facility": facility.name, "source_url": facility.url, "account": None, "score": 0, "result": "missing_account"})
            continue
        best_score, best_evidence, best = ranked[0]
        second_score = ranked[1][0] if len(ranked) > 1 else -1.0
        address_cluster = [a for a in candidates if same_physical_address(facility, a)]
        if address_cluster:
            best = max(address_cluster, key=lambda a: survivor_rank(facility, a, parent_id))
            best_score, best_evidence = address_score(facility, best)
        confident = bool(address_cluster) or (best_score >= 0.70 and best_score - second_score >= 0.10)
        if not confident:
            payload = desired_fields(facility, parent_id)
            proposals.append(make_proposal(
                "create", f"Create missing CRM account: {facility.name}", facility.slug, None, payload,
                [f"No confident CRM match; best candidate scored {best_score:.0%}"] + best_evidence,
                "high" if best_score < 0.45 else "medium", facility,
            ))
            matches.append({"facility": facility.name, "source_url": facility.url, "account": None, "score": best_score, "result": "missing_account"})
            continue

        matched_ids.add(best["account_id"])
        handled_ids.add(best["account_id"])
        desired = desired_fields(facility, parent_id)
        changes = changed_fields(best, desired)
        match_result = "confident_match"
        if not changes:
            current_id = best["account_id"]
        else:
            evidence = best_evidence + [f"CRM match score {best_score:.0%}"]
            wrong_parent = best.get("parent_id", "") != parent_id
            has_billing_hold = best.get("lifetime_revenue", 0) > 0 and best.get("outstanding_ar", 0) > 0
            if wrong_parent and has_billing_hold:
                new_payload = desired.copy()
                proposals.append(make_proposal(
                    "chow", f"CHOW: preserve old account and create current account for {facility.name}",
                    facility.slug, best,
                    {"create": new_payload, "old_patch": {"chow_current_account": "$NEW_ACCOUNT_ID"}},
                    evidence + ["Wrong parent", "Revenue history and outstanding AR require CHOW treatment"],
                    "high", facility,
                ))
                current_id = "$NEW_ACCOUNT_ID"
                match_result = "ownership_change"
            else:
                reason = "Re-parent and correct account" if wrong_parent else "Correct matched account fields"
                proposals.append(make_proposal(
                    "update", f"{reason}: {best['name']}", facility.slug, best, changes,
                    evidence + (["Wrong parent; direct re-parent allowed because CHOW hold does not apply"] if wrong_parent else []),
                    "high", facility,
                ))
                current_id = best["account_id"]
                match_result = "needs_fix"

        matches.append({"facility": facility.name, "source_url": facility.url, "account": best["name"], "account_id": best["account_id"], "score": best_score, "result": match_result})

        # Resolve additional records at the same facility address. These are
        # duplicates unless the billing-preservation SOP requires a CHOW link.
        for losing in address_cluster:
            if losing["account_id"] == best["account_id"]:
                continue
            handled_ids.add(losing["account_id"])
            billing_hold = losing.get("lifetime_revenue", 0) > 0 and losing.get("outstanding_ar", 0) > 0
            if billing_hold:
                if current_id != "$NEW_ACCOUNT_ID" and losing.get("chow_current_account") == current_id:
                    # Historical account is already preserved and points at the
                    # selected current record: the CHOW work is complete.
                    continue
                payload = {
                    "chow_current_account": current_id
                }
                kind = "chow_link"
                title = f"Preserve historical account and link current CHOW: {losing['name']}"
                evidence = ["Same physical facility as current website location", "Revenue history and outstanding AR require preservation"]
            else:
                payload = {
                    "duplicate_of_account": current_id,
                    "status": "Inactive",
                    "note": f"Duplicate of current account {current_id}; same normalized address and ZIP."
                }
                kind = "duplicate"
                title = f"Mark duplicate inactive: {losing['name']}"
                evidence = ["Same normalized address and ZIP as the selected survivor", f"Survivor: {best['name']} ({current_id})"]
            if all(losing.get(key, "") == value for key, value in payload.items()):
                continue
            proposals.append(make_proposal(kind, title, facility.slug + ":" + losing["account_id"], losing,
                                           payload, evidence, "high", facility))

    under_parent = [a for a in candidates if a.get("parent_id") == parent_id and a.get("status") != "Inactive"]
    for account in under_parent:
        if account["account_id"] in handled_ids:
            continue
        cross_parent = [a for a in candidates
                        if a["account_id"] != account["account_id"]
                        and a.get("parent_id") not in {"", parent_id}
                        and a.get("status") == "Active"
                        and not a.get("duplicate_of_account")
                        and normalize_street(a.get("billing_street", "")) == normalize_street(account.get("billing_street", ""))
                        and a.get("billing_zip") == account.get("billing_zip")]
        if len(cross_parent) == 1:
            current = cross_parent[0]
            billing_hold = account.get("lifetime_revenue", 0) > 0 and account.get("outstanding_ar", 0) > 0
            if billing_hold:
                payload = {"chow_current_account": current["account_id"]}
                if all(account.get(key, "") == value for key, value in payload.items()):
                    continue
                proposals.append(make_proposal(
                    "chow_link_existing", f"Link preserved historical account to current owner record: {account['name']}",
                    account["account_id"], account, payload,
                    ["Absent from Bellhaven website", f"Active cross-parent account at exact normalized address: {current['name']}",
                     "Revenue history and outstanding AR require the Bellhaven account to remain under its historical parent"],
                    "high",
                ))
            else:
                payload = {
                    "duplicate_of_account": current["account_id"],
                    "status": "Inactive",
                    "note": f"Duplicate historical record; current account is {current['name']} ({current['account_id']}) at the same normalized address and ZIP."
                }
                if all(account.get(key, "") == value for key, value in payload.items()):
                    continue
                proposals.append(make_proposal(
                    "duplicate", f"Link superseded Bellhaven record to current owner account: {account['name']}",
                    account["account_id"], account, payload,
                    ["Absent from Bellhaven website", f"Active cross-parent account at exact normalized address: {current['name']}",
                     "No outstanding-AR billing hold applies"],
                    "high",
                ))
            continue
        # Detect a losing duplicate by address against an already matched account.
        same_address = [a for a in candidates if a["account_id"] in matched_ids and
                        normalize_street(a.get("billing_street", "")) == normalize_street(account.get("billing_street", "")) and
                        a.get("billing_zip") == account.get("billing_zip")]
        if same_address:
            survivor = same_address[0]
            payload = {
                "duplicate_of_account": survivor["account_id"],
                "status": "Inactive",
                "note": f"Duplicate of {survivor['name']} ({survivor['account_id']}); same normalized address and ZIP."
            }
            proposals.append(make_proposal(
                "duplicate", f"Mark duplicate inactive: {account['name']}", account["account_id"], account,
                payload, [f"Same normalized address and ZIP as matched account {survivor['name']}", "Not listed separately on Bellhaven website"],
                "high",
            ))
        else:
            payload = {
                "status": "Needs Review",
                "note": "Ownership review: account is linked to Bellhaven but no current Bellhaven website location was found. Confirm sale/closure before removing parent relationship."
            }
            if all(account.get(key, "") == value for key, value in payload.items()):
                continue
            proposals.append(make_proposal(
                "stale", f"Flag Bellhaven-linked account absent from website: {account['name']}", account["account_id"], account,
                payload, ["Currently under Bellhaven parent", "No matching current website location", "Conservative action: preserve relationship pending confirmation"],
                "medium",
            ))

    classification_counts = {
        "confident_match": sum(x["result"] == "confident_match" for x in matches),
        "needs_fix": sum(x["result"] == "needs_fix" for x in matches),
        "missing_account": sum(x["result"] == "missing_account" for x in matches),
        "ownership_change": sum(x["result"] == "ownership_change" for x in matches),
        "absent_from_website": sum(p["kind"] in {"stale", "chow_link_existing"} for p in proposals),
        "duplicate": sum(p["kind"] == "duplicate" for p in proposals),
    }
    summary = {
        "website_facilities": len(facilities),
        "crm_accounts": len(accounts),
        "matched_website_facilities": sum(1 for x in matches if x["account"]),
        "missing_website_facilities": sum(1 for x in matches if not x["account"]),
        "proposals": len(proposals),
        "classification_counts": classification_counts,
        "bellhaven_parent_id": parent_id,
        "matches": matches,
    }
    return proposals, summary


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def run_pipeline(client: ApiClient) -> dict[str, Any]:
    facilities = scrape_facilities(client.base_url)
    accounts = client.accounts()
    save_json(ACCOUNT_INDEX_PATH, {a["account_id"]: a.get("name", "") for a in accounts})
    save_json(ACCOUNT_SNAPSHOT_PATH, {a["account_id"]: a for a in accounts})
    proposals, summary = build_proposals(facilities, accounts)
    decisions = load_json(DECISIONS_PATH, {})
    for proposal in proposals:
        if proposal["id"] in decisions:
            proposal["decision"] = decisions[proposal["id"]]
        else:
            proposal["decision"] = {"status": "pending"}
    summary["pending_proposals"] = sum(p["decision"]["status"] == "pending" for p in proposals)
    summary["previously_decided_findings"] = len(proposals) - summary["pending_proposals"]
    save_json(PROPOSALS_PATH, {"summary": summary, "proposals": proposals})
    return {"summary": summary, "proposals": proposals}


def apply_proposal(client: ApiClient, proposal: dict[str, Any]) -> dict[str, Any]:
    kind = proposal["kind"]
    if kind == "create":
        desired = proposal["payload"]
        for existing in client.accounts():
            if (normalize_name(existing.get("name", "")) == normalize_name(desired["name"])
                    and normalize_street(existing.get("billing_street", "")) == normalize_street(desired["billing_street"])
                    and existing.get("billing_zip") == desired["billing_zip"]
                    and existing.get("parent_id") == desired["parent_id"]):
                return {"idempotent_reuse": existing}
        return client.create_account(proposal["payload"])
    current = client.account(proposal["account_id"])
    before_version = proposal.get("account_before", {}).get("updated_at")
    if kind != "chow" and all(current.get(key, "") == value for key, value in proposal["payload"].items()):
        return {"idempotent_reuse": current}
    if kind == "chow" and current.get("chow_current_account"):
        return {"idempotent_reuse": current}
    if before_version and current.get("updated_at") != before_version:
        raise RuntimeError(
            "This CRM account changed after the proposal was generated. "
            "Refresh the analysis and review the new version before approving."
        )
    if kind == "chow":
        desired = proposal["payload"]["create"]
        current = next((a for a in client.accounts()
                        if normalize_name(a.get("name", "")) == normalize_name(desired["name"])
                        and normalize_street(a.get("billing_street", "")) == normalize_street(desired["billing_street"])
                        and a.get("billing_zip") == desired["billing_zip"]
                        and a.get("parent_id") == desired["parent_id"]), None)
        created = current or client.create_account(desired)
        new_id = created.get("account_id") or created.get("data", {}).get("account_id")
        if not new_id:
            raise RuntimeError(f"Create response did not include account_id: {created}")
        patch = {k: (new_id if v == "$NEW_ACCOUNT_ID" else v) for k, v in proposal["payload"]["old_patch"].items()}
        updated = client.patch_account(proposal["account_id"], patch)
        return {"created": created, "updated_old": updated}
    return client.patch_account(proposal["account_id"], proposal["payload"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile Bellhaven website facilities to CRM accounts")
    parser.add_argument("command", choices=["run", "summary"])
    args = parser.parse_args()
    if args.command == "run":
        result = run_pipeline(ApiClient())
    else:
        result = load_json(PROPOSALS_PATH, {})
    print(json.dumps(result.get("summary", result), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
