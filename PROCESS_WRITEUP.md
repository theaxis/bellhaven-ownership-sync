# Bellhaven Ownership Sync: Process and Rationale

## What I built

I built a reconciliation pipeline that compares Bellhaven's website directory with the CRM, proposes changes, and requires a person to approve each write. It scrapes every community page, matches each facility to the most likely CRM account, and classifies the result as a confident match, correction, ownership change, missing account, duplicate, or Bellhaven account absent from the website.

The website is the reference for Bellhaven’s portfolio, while the CRM remains the system of record for account and billing history. I treat discrepancies as evidence to investigate, not proof that either source is automatically correct. A website address can establish which facility an account represents, but the CRM's revenue and outstanding AR determine whether that account can be re-parented or must remain as a historical billing record.

## Matching approach

The three-page directory contained 34 unique facilities. For each one, the scraper records the name, full address, care offerings, phone number, source URL, and URL slug. The CRM started with 121 accounts and one Bellhaven parent.

The matcher is deterministic. It normalizes names and addresses, then scores ZIP code, city and state, street address, and name. Address carries the most weight because names often change when a facility changes owners. Name similarity supports a match but cannot override a conflicting address.

My first dry run exposed a flaw in the confidence-margin rule. Several facilities had two or three CRM records at the same normalized address. Those records received the same high score, so the tie looked like “no match.” The pipeline would have created a new account and separately flagged the real account as stale.

To address this, I changed same-address ties into record clusters. Within a cluster, the matcher chooses a survivor using the website name, Bellhaven parent link, active and duplicate status, care type, and billing history. Other records at that address become duplicate or CHOW candidates. That change raised confident matches from 25 to 30 and reduced genuinely missing website facilities from nine to four.

I also stopped the app from proposing changes whose only difference was `Road` versus `Rd`, `Lane` versus `Ln`, or a directional abbreviation. Those edits did not improve identity, routing, or ownership accuracy and created unnecessary churn. I treated the website name as evidence of the facility's current branding and surfaced meaningful name differences for human review.

## Ownership and billing decisions

A wrong-parent match normally produces a partial update to `parent_id`, plus any supported name or field correction. Before proposing that update, the pipeline checks `lifetime_revenue` and `outstanding_ar`. If both are positive, it preserves the historical account under its old parent, creates or reuses a current account under Bellhaven, and changes only the historical record's `chow_current_account` field. Tiffin and Marietta followed this CHOW path. Other wrong-parent records without the billing hold could be re-parented directly.

Website absence alone does not prove a sale or closure. Four Bellhaven-linked CRM records are absent from the current directory. Owosso is an inactive duplicate. Sandusky has an active Millstone account at the exact same address, so I preserved the revenue-bearing Bellhaven record and linked it to that existing current-owner account. Alliance and Coldwater had no credible successor elsewhere in the CRM and remain under Bellhaven as `Needs Review`.

I also checked the evidence beyond account names. Alliance and Coldwater have no CRM contacts. Historical Sandusky has one active contact with a Bellhaven email address, while the Millstone record has none. I treated that as possibly stale contact data rather than stronger evidence than the exact-address successor account. I also checked every Millstone-linked account; Sandusky is the only one, and neither Alliance nor Coldwater shares an address, ZIP code, phone number, or meaningful name similarity with it. I left contact cleanup outside the assignment's account scope.

The CRM finished with 127 accounts: four missing website facilities plus two new current accounts required by CHOW. A fresh run matches all 34 website facilities to active Bellhaven accounts and produces zero pending proposals. The two unresolved accounts remain visible in the app as persistent flags. Resolved duplicate and CHOW exceptions remain available separately for audit.

## Review and safe reruns

The local app shows the website evidence, current CRM record, proposed values, confidence, and a readable before-and-after comparison. Pending items are grouped by business category. Approvals, rejections, reviewer names, optional rationales, and superseded decisions are retained in history. Record names link to a read-only CRM detail page so a reviewer can move from a summary to the underlying account.

Analysis and refresh never write to the CRM. The write path is available only after approval and uses partial updates. Immediately before an update, the app reloads the account and compares `updated_at` with the version the reviewer saw. If the record changed in the meantime, the approval stops and requires a refreshed proposal. If the proposed values are already present, the action finishes without another write.

Proposal IDs are deterministic hashes of the action and payload. An unchanged rejected proposal therefore remains decided on the next run. Applied corrections, duplicate links, stale flags, creates, and CHOW links also disappear when the current CRM already has the intended state, even if the runner does not have the local decision file. Retried create and CHOW operations search for an equivalent current account before creating one, which reduces duplicate risk after a partial failure.

The included cron entry runs from the same persistent checkout as the review app so the local decision ledger survives between runs. The scheduled script also runs a health check. It compares the directory's declared count with the number of unique facilities scraped, checks required fields and duplicate source identities, validates parent, duplicate, and CHOW references, and confirms that every published facility has a current active Bellhaven account. A structural failure or unexpected portfolio change exits nonzero and leaves a machine-readable report.

## How I used AI

I used Codex to inspect the website and API shapes, implement the scraper and review app, generate tests, challenge matching assumptions, and keep the documentation current. No LLM runs inside the pipeline. The matching rules are deterministic, the evidence is visible, and every CRM write requires human approval. I reviewed the proposals and API results before accepting the final CRM state.

## What I would build next

For production, I would move decisions, proposal snapshots, and run history from local JSON into a shared database. I would add authenticated reviewer identities and role-based access, then pair the current stale-write check with API-level conditional writes or idempotency keys. I would also store field-level source provenance, alert on ambiguous clusters and unusual proposal volume, and add a separate workflow that checks a second authoritative source before confirming a sale, closure, or parent change.
