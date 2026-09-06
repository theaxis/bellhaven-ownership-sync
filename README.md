# Bellhaven Ownership Sync

Bellhaven Ownership Sync reconciles the fictional Bellhaven Senior Living website with its CRM sandbox. It scrapes every published community, matches locations primarily by normalized physical address, identifies corrections and ownership changes, and presents every proposed mutation in a local human-review app. The CRM is never changed by analysis alone.

The accompanying rationale, including the matching decisions and validation evidence, is in [`PROCESS_WRITEUP.md`](PROCESS_WRITEUP.md).

## Requirement checklist

- **Corrected CRM:** The approved reconciliation has been applied to the assessment sandbox. All 34 published Bellhaven facilities have active current accounts under Bellhaven, and a fresh run produces zero pending proposals.
- **Scraper:** Every community detail page supplies name, street, city, state, ZIP, and care offerings; phone and source URL are also retained as evidence.
- **Matching and classification:** The pipeline handles confident matches, field corrections, direct re-parenting, missing accounts, duplicate records, CHOW preservation, and Bellhaven children absent from the website. The app shows current classification counts and groups pending work by business category.
- **Human review:** Only pending findings appear in the review queue. Each shows source evidence, current CRM values, proposed field changes, confidence, and rationale controls. Only approval can call a write endpoint; completed decisions move to history.
- **Daily safe reruns:** [`config/crontab.example`](config/crontab.example) invokes the pipeline and monitor from one persistent checkout. The local decision ledger therefore remembers unchanged rejections, while applied changes are also suppressed by current-CRM state checks.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export BELLHAVEN_API_TOKEN="your-token"
python app.py
```

Open `http://127.0.0.1:8000`. The home dashboard summarizes the latest run and persistent flags; its Home control is separate from Refresh analysis. Review the website evidence, current CRM record, and readable field-level change, then approve or reject each item on the dedicated Pending decisions screen. Pending proposals are grouped as missing accounts, matched-account corrections, ownership changes, duplicates, or Bellhaven accounts absent from the website. The headline counts are navigation: website facilities opens the complete match list, pending opens its own decision screen, approved or rejected opens the corresponding spreadsheet-style decision log, and flagged records opens a persistent exception workspace. Any in-scope account with CRM status `Needs Review` appears there regardless of the reason; related website-absence, duplicate, and historical CHOW outcomes remain available through quick filters. The exception table can be sorted by account, status, disposition, reason, or latest decision time. This keeps safe no-op reruns from hiding concerns or inflating the actionable count. Facility names are clickable throughout these views and open a read-only CRM record with its source page and related decision explanation. Filtered logs show facility, category, change made, fields affected, decision time, and reviewer; the time heading toggles newest- or oldest-first. The separate Decision history view preserves approvals, rejections, and superseded outcomes together as a chronological narrative. Set `BELLHAVEN_REVIEWER_NAME` to record the responsible reviewer without adding an unnecessary authentication system to the local assessment.

The token belongs in the environment or a repository secret, never in source control.

## Matching approach

The website is the reference for Bellhaven's publicly listed portfolio, while the CRM remains the system of record for account and billing history. Discrepancies are evidence to investigate, not proof that either source is automatically correct. Matching emphasizes normalized street, ZIP, and city/state because names change after acquisitions. Names provide supporting evidence, not the sole identity key. A match must clear a confidence threshold and be separated from the next-best candidate; otherwise the pipeline proposes a new account.

For matched facilities, the pipeline proposes corrections to meaningful name, address, care-type, status, and parent discrepancies. Cosmetic address differences such as `Road` versus `Rd` are normalized for comparison but do not create proposals. Active CRM accounts currently under Bellhaven but absent from the website are conservatively marked `Needs Review` with an explanatory note. They are not automatically detached because absence alone does not prove a sale or closure. An extra CRM account at an already-matched address is marked `Inactive` and linked through `duplicate_of_account`.

## CHOW safeguard

Before re-parenting, the pipeline checks both `lifetime_revenue` and `outstanding_ar`. When both are greater than zero, it preserves every old-account field, creates a current account under Bellhaven, and then changes only `chow_current_account` on the old record. Otherwise it re-parents the existing account directly. This rule is isolated in proposal generation and displayed explicitly in review evidence.

## Safe daily operation

`data/decisions.json` is an append-style decision ledger keyed by a deterministic proposal fingerprint. A refresh is read-only and carries forward previous decisions. Approved changes also alter the CRM state, so they naturally disappear from later diffs. Rejected proposals remain suppressed while their proposed payload is unchanged. API updates use partial `PATCH` payloads; analysis does not write.

Immediately before a write, the approval path reloads the target account and compares its `updated_at` value with the version shown to the reviewer. If another process changed the account in the meantime, approval stops and requires a refreshed proposal. If the exact payload is already present, the action resolves as an idempotent no-op instead of failing or writing again.

The included cron configuration is deliberately paired with a persistent checkout because the assessment's local review app stores decisions in `data/decisions.json`. This ensures an unchanged rejected proposal remains decided on the next scheduled run. `scripts/run_daily.sh` runs both analysis and monitoring and can load the ignored local `.env`; that file should be readable only by the service account. In a distributed production version, decisions and proposal snapshots would instead live in a durable shared database, authentication would use managed secrets, and API-level idempotency keys or conditional writes would complement the application safeguards.

## Health monitor

```bash
python monitor.py
```

The scheduled runner invokes this monitor after reconciliation analysis. It verifies the declared directory count against the number actually scraped, checks a reviewed count baseline and reasonable bounds, requires all source identity fields, detects duplicate source keys and addresses, requires exactly one Bellhaven parent, checks parent/duplicate/CHOW referential integrity, and confirms every published facility has an active Bellhaven child account. Structural failures and meaningful portfolio changes exit non-zero instead of quietly accepting partial data. The detailed result is written to `data/health_report.json`.

## AI use

I used Codex to inspect the source and CRM shapes, implement the scraper and review workflow, pressure-test entity-matching rules, and create tests and documentation. I retained human control over CRM decisions and verified the generated logic against the actual sandbox data, especially ambiguous names, duplicate addresses, and the CHOW billing condition.

## Tests

```bash
python -m unittest discover -s tests -v
```
