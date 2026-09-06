"""Local human-review application for Bellhaven reconciliation proposals."""

from __future__ import annotations

import html
import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from bellhaven_sync import (
    ACCOUNT_INDEX_PATH, ACCOUNT_SNAPSHOT_PATH, ApiClient, DECISIONS_PATH, PROPOSALS_PATH,
    apply_proposal, load_json,
    run_pipeline, save_json,
)


CSS = """
:root{--ink:#17231f;--green:#2e5d50;--gold:#c9a227;--cream:#faf7f0;--line:#dcd8cd;--red:#9b2c2c}
*{box-sizing:border-box}body{margin:0;background:var(--cream);color:var(--ink);font:15px/1.45 system-ui,sans-serif}
header{background:var(--green);color:white;padding:20px max(24px,calc((100% - 1120px)/2))}header h1{margin:0;font:27px Georgia,serif}
main{max-width:1120px;margin:auto;padding:24px}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}.stat,.card{background:white;border:1px solid var(--line);border-radius:8px;padding:16px}.stat{color:var(--ink);text-decoration:none}.stat:hover{border-color:var(--green);box-shadow:0 2px 8px #00000012}.stat b{display:block;font-size:25px;color:var(--green)}
.toolbar{display:flex;gap:10px;margin:18px 0;align-items:center;flex-wrap:wrap}.button,button{border:0;border-radius:5px;padding:9px 13px;background:var(--green);color:white;cursor:pointer;text-decoration:none}.button.secondary{background:#59645f}.reject{background:#666}.pill{padding:3px 8px;border-radius:99px;background:#eee;font-size:12px}.high{background:#dff2e8}.medium{background:#fff0c7}.card{margin:12px 0}.card h2{font-size:18px;margin:0 0 8px}.meta{color:#696d69;font-size:13px}.evidence{margin:10px 0}.columns{display:grid;grid-template-columns:1fr 1fr;gap:14px}.box{background:#f7f7f4;border-radius:5px;padding:10px;white-space:pre-wrap;font:12px/1.35 ui-monospace,monospace;overflow:auto}.actions{display:flex;gap:8px;margin-top:12px}.actions form{width:100%}.reason{width:100%;padding:9px;border:1px solid var(--line);border-radius:5px;margin:8px 0}.decided{opacity:.72}.error{background:#ffe0e0;color:var(--red);padding:12px;border-radius:6px}.ok{background:#dff2e8;padding:12px;border-radius:6px}.classifications{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:12px 0}.classification{background:white;border:1px solid var(--line);border-radius:7px;padding:10px}.classification b{display:block;color:var(--green);font-size:20px}.group-title{margin:24px 0 6px;font:20px Georgia,serif;color:var(--green)}.table-wrap{overflow-x:auto}.diff,.facility-table{width:100%;border-collapse:collapse;margin-top:6px}.diff th,.diff td,.facility-table th,.facility-table td{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}.diff th,.facility-table th{font-size:12px;color:#667}.facility-table tbody tr:hover{background:#faf8f2}.before{color:#7b3434}.after{color:#205d47;font-weight:600}.history-row{display:grid;grid-template-columns:145px 1fr;gap:18px;padding:15px 0;border-bottom:1px solid var(--line)}.history-row h3{margin:4px 0;font-size:16px}.history-date{color:#696d69;font-size:13px}.history-meta{color:#777;font-size:12px;margin-top:5px}.history-comment{margin-top:7px;padding:7px 9px;background:#f4f1e9;border-left:3px solid var(--gold)}@media(max-width:750px){.stats,.columns,.classifications{grid-template-columns:1fr}.history-row{grid-template-columns:1fr;gap:4px}.facility-table{font-size:12px}}
"""

CATEGORY_ORDER = {
    "Missing CRM accounts": 0,
    "Matched accounts needing correction": 1,
    "Ownership changes": 2,
    "Duplicate records": 3,
    "Bellhaven accounts absent from the website": 4,
}


def proposal_category(proposal: dict) -> str:
    kind = proposal.get("kind")
    if kind == "create":
        return "Missing CRM accounts"
    if kind == "update":
        return "Matched accounts needing correction"
    if kind in {"chow", "chow_link", "chow_link_existing"}:
        return "Ownership changes"
    if kind == "duplicate":
        return "Duplicate records"
    return "Bellhaven accounts absent from the website"


def esc(value) -> str:
    return html.escape(str(value))


def render_diff(proposal: dict) -> str:
    payload = proposal.get("payload", {})
    before = proposal.get("account_before", {})
    rows = []
    if proposal.get("kind") == "chow":
        for key, value in payload.get("create", {}).items():
            rows.append(f'<tr><td>New current account</td><td>{esc(key)}</td><td class="before">—</td><td class="after">{esc(value)}</td></tr>')
        for key, value in payload.get("old_patch", {}).items():
            old = before.get(key, "—")
            rows.append(f'<tr><td>Historical account</td><td>{esc(key)}</td><td class="before">{esc(old)}</td><td class="after">{esc(value)}</td></tr>')
        headings = "<th>Record</th><th>Field</th><th>Current</th><th>Proposed</th>"
    else:
        for key, value in payload.items():
            old = before.get(key, "—") if before else "—"
            rows.append(f'<tr><td>{esc(key)}</td><td class="before">{esc(old)}</td><td class="after">{esc(value)}</td></tr>')
        headings = "<th>Field</th><th>Current</th><th>Proposed</th>"
    return f'<table class="diff"><thead><tr>{headings}</tr></thead><tbody>' + ''.join(rows) + '</tbody></table>'


def friendly_fields(fields: list[str]) -> str:
    labels = {
        "name": "name", "parent_id": "parent company", "parent_name": "parent-company label",
        "billing_street": "street address", "billing_city": "city", "billing_state": "state",
        "billing_zip": "ZIP code", "care_type": "care offerings", "status": "status",
        "note": "review note", "duplicate_of_account": "duplicate link",
        "chow_current_account": "current-owner link",
    }
    return ", ".join(labels.get(field, field.replace("_", " ")) for field in fields)


def outcome_summary(decision: dict, account_names: dict[str, str] | None = None) -> tuple[str, str, str]:
    account_names = account_names or {}
    proposal = decision.get("proposal", {})
    if proposal:
        return proposal.get("kind", "decision"), proposal.get("title", "Reviewed proposal"), proposal.get("account_id", "")
    result = decision.get("result", {})
    if "created" in result:
        record = result.get("created", {})
        account_id = record.get("account_id", "")
        name = account_names.get(account_id, "current facility account")
        return "CHOW", f"Created {name} and linked the historical account", account_id
    record = result.get("idempotent_reuse", result) if isinstance(result, dict) else {}
    changed = record.get("fields", [])
    if record.get("duplicate_of_account") or "duplicate_of_account" in changed:
        action = "duplicate"
    elif record.get("status") == "Needs Review" or set(changed) == {"note", "status"}:
        action = "needs review"
    elif record.get("created_by_candidate") or record.get("message") == "created":
        action = "create"
    elif record.get("chow_current_account") or "chow_current_account" in changed:
        action = "chow link"
    else:
        action = "update"
    account_id = record.get("account_id", "")
    name = record.get("name") or account_names.get(account_id) or "CRM account"
    if action == "duplicate":
        title = f"Marked {name} as a duplicate and inactive"
    elif action == "needs review":
        title = f"Flagged {name} for ownership review"
    elif action == "create":
        title = f"Created {name}"
    elif action == "chow link":
        title = f"Linked historical {name} to its current-owner account"
    elif changed:
        title = f"Updated {name}: {friendly_fields(changed)}"
    else:
        title = f"Approved change to {name}"
    return action, title, account_id


def readable_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.strftime("%b %-d, %Y · %H:%M UTC")
    except (TypeError, ValueError):
        return value


def readable_match_result(value: str) -> str:
    return {
        "confident_match": "Confident match",
        "needs_fix": "Needs correction",
        "missing_account": "Missing CRM account",
        "ownership_change": "Ownership change",
    }.get(value, value.replace("_", " ").title())


def decision_fields(decision: dict) -> list[str]:
    proposal = decision.get("proposal", {})
    if proposal:
        payload = proposal.get("payload", {})
        if proposal.get("kind") == "chow":
            return list(dict.fromkeys([*payload.get("create", {}), *payload.get("old_patch", {})]))
        return list(payload)
    result = decision.get("result", {})
    if "created" in result:
        return list(dict.fromkeys([
            *result.get("created", {}).get("fields", []),
            *result.get("updated_old", {}).get("fields", []),
        ]))
    record = result.get("idempotent_reuse", result) if isinstance(result, dict) else {}
    return record.get("fields", [])


def record_fields(record: dict) -> list[tuple[str, object]]:
    labels = {
        "name": "Account name", "status": "Status", "parent_name": "Parent company",
        "billing_street": "Street", "billing_city": "City", "billing_state": "State",
        "billing_zip": "ZIP code", "care_type": "Care offerings", "phone": "Phone",
        "lifetime_revenue": "Lifetime revenue", "outstanding_ar": "Outstanding AR",
        "duplicate_of_account": "Duplicate of", "chow_current_account": "Current CHOW account",
        "note": "CRM note", "account_id": "Account ID", "updated_at": "Last updated",
    }
    order = [
        "name", "status", "parent_name", "billing_street", "billing_city", "billing_state",
        "billing_zip", "care_type", "phone", "lifetime_revenue", "outstanding_ar",
        "duplicate_of_account", "chow_current_account", "note", "account_id", "updated_at",
    ]
    return [(labels[key], record.get(key, "—") if record.get(key, "") != "" else "—") for key in order]


def website_absent_disposition(record: dict, account_names: dict[str, str]) -> tuple[str, str]:
    duplicate_id = record.get("duplicate_of_account")
    current_id = record.get("chow_current_account")
    if duplicate_id:
        return "Inactive duplicate", f'Duplicate of {account_names.get(duplicate_id, duplicate_id)}.'
    if current_id:
        return "Historical CHOW record", f'Linked to current account {account_names.get(current_id, current_id)}.'
    if record.get("status") == "Needs Review":
        return "Needs ownership review", "No supported successor was found; Bellhaven ownership is retained pending confirmation."
    return "Unresolved website absence", "The account remains under Bellhaven but is not in the current website directory."


def persistent_flag_reason(record: dict, absent_ids: set[str]) -> str:
    if record.get("account_id") in absent_ids:
        return "Absent from the website; no supported successor was found and ownership remains unconfirmed."
    return "The CRM status requires manual review."


def page(
    message: str = "", view: str = "dashboard", decision_filter: str = "",
    sort_order: str = "newest", account_id: str = "", exception_filter: str = "open",
    exception_sort: str = "name", exception_order: str = "asc",
) -> str:
    data = load_json(PROPOSALS_PATH, {"summary": {}, "proposals": []})
    all_decisions = load_json(DECISIONS_PATH, {})
    account_names = load_json(ACCOUNT_INDEX_PATH, {})
    account_snapshot = load_json(ACCOUNT_SNAPSHOT_PATH, {})
    summary, proposals = data.get("summary", {}), data.get("proposals", [])
    pending = [p for p in proposals if p.get("decision", {}).get("status", "pending") == "pending"]
    approved = sum(d.get("status") == "approved" for d in all_decisions.values())
    rejected = sum(d.get("status") == "rejected" for d in all_decisions.values())
    matched_ids = {match.get("account_id") for match in summary.get("matches", [])}
    bellhaven_parent_id = summary.get("bellhaven_parent_id")
    absent_records = sorted(
        (
            record for record in account_snapshot.values()
            if bellhaven_parent_id
            and record.get("parent_id") == bellhaven_parent_id
            and record.get("account_id") not in matched_ids
        ),
        key=lambda record: record.get("name", ""),
    )
    absent_ids = {record.get("account_id") for record in absent_records}
    scoped_ids = matched_ids | {
        record.get("account_id") for record in account_snapshot.values()
        if bellhaven_parent_id and record.get("parent_id") == bellhaven_parent_id
    }
    flagged_records = sorted(
        (
            record for record in account_snapshot.values()
            if record.get("account_id") in scoped_ids and record.get("status") == "Needs Review"
        ),
        key=lambda record: record.get("name", ""),
    )
    cards = []
    history_records = []
    previous_category = ""
    ordered_pending = sorted(pending, key=lambda p: (CATEGORY_ORDER[proposal_category(p)], p.get("title", "")))
    for p in ordered_pending if view == "pending" else []:
        category = proposal_category(p)
        if category != previous_category:
            cards.append(f'<h2 class="group-title">{esc(category)}</h2>')
            previous_category = category
        status = p.get("decision", {}).get("status", "pending")
        source = p.get("source", {})
        evidence = "".join(f"<li>{esc(x)}</li>" for x in p.get("evidence", []))
        actions = ""
        if status == "pending":
            actions = f'''<div class="actions"><form method="post" action="/decide"><input type="hidden" name="id" value="{esc(p['id'])}"><input class="reason" name="comment" maxlength="500" placeholder="Optional review rationale"><button name="decision" value="approved">Approve &amp; apply</button> <button class="reject" name="decision" value="rejected">Reject</button></form></div>'''
        cards.append(f'''<article class="card {'decided' if status != 'pending' else ''}">
          <h2>{esc(p['title'])}</h2><div class="meta">{esc(p['kind'])} · <span class="pill {esc(p['confidence'])}">{esc(p['confidence'])} confidence</span> · {esc(status)}</div>
          <ul class="evidence">{evidence}</ul>
          <div class="columns"><div><b>Website evidence</b><div class="box">{esc(json.dumps(source,indent=2))}</div></div><div><b>Current CRM record</b><div class="box">{esc(json.dumps(p.get('account_before',{}),indent=2))}</div></div></div>
          <div style="margin-top:10px"><b>Field-level change</b>{render_diff(p)}</div>{actions}</article>''')
    if view == "history":
        later_actions: dict[str, str] = {}
        history_items = [(proposal_id, decision) for proposal_id, decision in all_decisions.items()
                         if not decision_filter or decision.get("status") == decision_filter]
        for proposal_id, decision in sorted(history_items, key=lambda item: item[1].get("decided_at", ""), reverse=True):
            action, title, decision_account_id = outcome_summary(decision, account_names)
            comment = decision.get("comment")
            decided_at = readable_timestamp(decision.get("decided_at", ""))
            rationale = f'<div class="history-comment"><b>Reviewer note:</b> {esc(comment)}</div>' if comment else ""
            superseded = action == "needs review" and later_actions.get(decision_account_id) in {"chow link", "CHOW"}
            status_text = "Superseded" if superseded else decision.get("status", "").title()
            status_class = "medium" if superseded or decision.get("status") != "approved" else "high"
            resolution = '<div class="meta">Replaced by the later CHOW resolution shown above.</div>' if superseded else ""
            reviewer = decision.get("reviewer") or os.getenv("BELLHAVEN_REVIEWER_NAME")
            reviewer_text = f" · Reviewer {esc(reviewer)}" if reviewer else ""
            history_title = (
                f'<a href="/?view=account&amp;id={esc(decision_account_id)}">{esc(title)}</a>'
                if decision_account_id else esc(title)
            )
            cards.append(f'''<div class="history-row"><div class="history-date">{esc(decided_at)}</div><div><span class="pill">{esc(action)}</span> <span class="pill {status_class}">{esc(status_text)}</span><h3>{history_title}</h3>{resolution}{rationale}<div class="history-meta">Account {esc(decision_account_id)} · Decision {esc(proposal_id)}{reviewer_text}</div></div></div>''')
            history_records.append({
                "account_id": decision_account_id,
                "facility": account_names.get(decision_account_id) or decision.get("proposal", {}).get("source", {}).get("name") or "CRM account",
                "action": action,
                "title": title,
                "fields": friendly_fields(decision_fields(decision)) or "New account",
                "decided_at": decided_at,
                "decided_raw": decision.get("decided_at", ""),
                "reviewer": reviewer or "—",
                "status": status_text,
            })
            if decision_account_id:
                later_actions.setdefault(decision_account_id, action)
    notice = f'<div class="ok">{esc(message)}</div>' if message else ""
    counts = summary.get("classification_counts", {})
    count_labels = [
        ("confident_match", "Confident matches"), ("needs_fix", "Need correction"),
        ("missing_account", "Missing accounts"), ("ownership_change", "Ownership changes"),
        ("duplicate", "Duplicate proposals"), ("absent_from_website", "Absent-site proposals"),
    ]
    classification_html = '<h2 class="group-title">Latest run</h2><section class="classifications">' + ''.join(
        f'<div class="classification"><b>{counts.get(key, 0)}</b>{esc(label)}</div>' for key, label in count_labels
    ) + '</section>'
    facility_rows = ''.join(
        f'''<tr><td><b><a href="/?view=account&amp;id={esc(match.get("account_id", ""))}">{esc(match.get("facility", ""))}</a></b></td><td>{esc(match.get("account") or "—")}</td><td>{esc(readable_match_result(match.get("result", "")))}</td><td>{match.get("score", 0):.0%}</td></tr>'''
        for match in sorted(summary.get("matches", []), key=lambda item: item.get("facility", ""))
    )
    facilities_html = f'''<div class="card"><h2>Website facilities</h2><div class="meta">Every scraped Bellhaven location and its current CRM classification.</div><div class="table-wrap"><table class="facility-table"><thead><tr><th>Website facility</th><th>Matched CRM account</th><th>Classification</th><th>Match score</th></tr></thead><tbody>{facility_rows}</tbody></table></div></div>'''
    resolved_absent_records = [record for record in absent_records if record.get("status") != "Needs Review"]
    flagged_count = len(flagged_records)
    flag_noun = "record" if flagged_count == 1 else "records"
    watchlist_summary = f'''<div class="card"><h2><a href="/?view=exceptions&amp;filter=open">{flagged_count} flagged {flag_noun}</a></h2><div class="meta">Needs Review flags remain persistently visible even when a safe rerun produces no new proposal. {len(absent_records)} Bellhaven-linked CRM records are absent from the website in total; {len(resolved_absent_records)} already have a duplicate or CHOW disposition.</div></div>'''
    pending_empty = '<div class="card"><h2>Pending decisions</h2><div class="meta">No pending decisions.</div></div>'
    pending_html = pending_empty if not pending else f'''<div class="card"><h2>Pending decisions</h2><div class="meta">{len(pending)} proposal{"s" if len(pending) != 1 else ""} require a reviewer decision. Open each item to inspect its evidence before approving or rejecting it.</div></div>{''.join(cards)}'''

    latest_decision: dict[str, str] = {}
    for decision in all_decisions.values():
        _, _, decision_account_id = outcome_summary(decision, account_names)
        decided_raw = decision.get("decided_at", "")
        if decision_account_id and decided_raw > latest_decision.get(decision_account_id, ""):
            latest_decision[decision_account_id] = decided_raw
    exception_records = []
    exception_ids = {record.get("account_id") for record in flagged_records}
    for record in [*flagged_records, *(record for record in resolved_absent_records if record.get("account_id") not in exception_ids)]:
        is_open = record.get("status") == "Needs Review"
        disposition, reason = (
            ("Needs review", persistent_flag_reason(record, absent_ids))
            if is_open else website_absent_disposition(record, account_names)
        )
        category = "duplicate" if record.get("duplicate_of_account") else "chow" if record.get("chow_current_account") else "needs_review"
        exception_records.append({
            "account_id": record.get("account_id", ""), "name": record.get("name", "CRM account"),
            "status": record.get("status", "—"), "disposition": disposition, "reason": reason,
            "group": "open" if is_open else "resolved", "category": category,
            "website_absent": record.get("account_id") in absent_ids,
            "decided_raw": latest_decision.get(record.get("account_id", ""), ""),
        })
    filter_counts = {
        "open": sum(item["group"] == "open" for item in exception_records),
        "resolved": sum(item["group"] == "resolved" for item in exception_records),
        "all": len(exception_records),
        "website_absent": sum(item["website_absent"] for item in exception_records),
        "duplicate": sum(item["category"] == "duplicate" for item in exception_records),
        "chow": sum(item["category"] == "chow" for item in exception_records),
    }
    filtered_exceptions = [
        item for item in exception_records
        if exception_filter == "all"
        or exception_filter == item["group"]
        or exception_filter == "website_absent" and item["website_absent"]
        or exception_filter == item["category"]
    ]
    exception_sort_keys = {
        "name": lambda item: item["name"].lower(), "status": lambda item: item["status"].lower(),
        "type": lambda item: item["disposition"].lower(), "reason": lambda item: item["reason"].lower(),
        "decision": lambda item: item["decided_raw"],
    }
    filtered_exceptions.sort(key=exception_sort_keys[exception_sort], reverse=exception_order == "desc")
    filter_labels = [
        ("open", "Open"), ("resolved", "Resolved"), ("all", "All"),
        ("website_absent", "Website absent"), ("duplicate", "Duplicates"), ("chow", "CHOW"),
    ]
    exception_filters = '<div class="toolbar">' + ''.join(
        f'<a class="button {"" if key == exception_filter else "secondary"}" href="/?view=exceptions&amp;filter={key}">{label} ({filter_counts[key]})</a>'
        for key, label in filter_labels
    ) + '</div>'

    def exception_sort_link(key: str, label: str) -> str:
        next_direction = "desc" if exception_sort == key and exception_order == "asc" else "asc"
        arrow = " ↑" if exception_sort == key and exception_order == "asc" else " ↓" if exception_sort == key else ""
        return f'<a href="/?view=exceptions&amp;filter={esc(exception_filter)}&amp;sort={key}&amp;direction={next_direction}">{label}{arrow}</a>'

    exception_rows = "".join(
        f'''<tr><td><b><a href="/?view=account&amp;id={esc(item["account_id"])}">{esc(item["name"])}</a></b></td><td>{esc(item["status"])}</td><td><span class="pill">{esc(item["disposition"])}</span></td><td>{esc(item["reason"])}</td><td>{esc(readable_timestamp(item["decided_raw"]) if item["decided_raw"] else "—")}</td></tr>'''
        for item in filtered_exceptions
    )
    exception_heading = dict(filter_labels)[exception_filter]
    exception_table = f'''<div class="table-wrap"><table class="facility-table"><thead><tr><th>{exception_sort_link("name", "CRM account")}</th><th>{exception_sort_link("status", "Status")}</th><th>{exception_sort_link("type", "Disposition")}</th><th>{exception_sort_link("reason", "Reason")}</th><th>{exception_sort_link("decision", "Last decision")}</th></tr></thead><tbody>{exception_rows}</tbody></table></div>'''
    absent_html = f'''<div class="card"><h2>Flagged records and exceptions</h2><div class="meta">Open flags remain visible until their CRM status is resolved. Filters also retain related duplicate and CHOW outcomes for investigation.</div>{exception_filters}<h3>{esc(exception_heading)}</h3>{exception_table}</div>'''
    history_heading = f'{esc(decision_filter.title())} decisions' if decision_filter else 'Decision history'
    ordered_history_records = sorted(
        history_records,
        key=lambda item: item["decided_raw"],
        reverse=sort_order != "oldest",
    )
    decision_rows = ''.join(
        f'''<tr><td><b><a href="/?view=account&amp;id={esc(record["account_id"])}">{esc(record["facility"])}</a></b></td><td><span class="pill">{esc(record["action"])}</span></td><td>{esc(record["title"])}</td><td>{esc(record["fields"])}</td><td>{esc(record["decided_at"])}</td><td>{esc(record["reviewer"])}</td></tr>'''
        for record in ordered_history_records
    )
    next_order = "oldest" if sort_order != "oldest" else "newest"
    sort_arrow = "↓" if sort_order != "oldest" else "↑"
    sort_url = f'/?view=history&amp;status={esc(decision_filter)}&amp;order={next_order}'
    decision_table = f'''<div class="meta">Saved outcomes are shown as recorded. Full before-values were not retained for approvals completed before proposal snapshots were added.</div><div class="table-wrap"><table class="facility-table"><thead><tr><th>Facility</th><th>Category</th><th>Change made</th><th>Fields affected</th><th><a href="{sort_url}">Decision time {sort_arrow}</a></th><th>Reviewer</th></tr></thead><tbody>{decision_rows}</tbody></table></div>'''
    history_body = (decision_table if decision_filter and history_records else ''.join(cards)) or f'<div class="meta">No {esc(decision_filter or "recorded")} decisions.</div>'
    selected_id = account_id
    selected_record = account_snapshot.get(selected_id, {})
    record_rows = ''.join(f'<tr><th>{esc(label)}</th><td>{esc(value)}</td></tr>' for label, value in record_fields(selected_record))
    related = []
    for proposal_id, decision in sorted(all_decisions.items(), key=lambda item: item[1].get("decided_at", ""), reverse=True):
        action, title, related_id = outcome_summary(decision, account_names)
        if related_id == selected_id:
            related.append(f'<div class="history-row"><div class="history-date">{esc(readable_timestamp(decision.get("decided_at", "")))}</div><div><span class="pill">{esc(action)}</span><h3>{esc(title)}</h3><div class="history-meta">{esc(decision.get("status", "")).title()} · Decision {esc(proposal_id)}</div></div></div>')
    source_match = next((match for match in summary.get("matches", []) if match.get("account_id") == selected_id), {})
    source_link = f'<a href="{esc(source_match.get("source_url"))}" target="_blank" rel="noreferrer">Open website facility page</a>' if source_match.get("source_url") else ""
    decision_section = ''.join(related) or '<div class="meta">No CRM change was required for this confident match.</div>'
    account_html = f'''<div class="card"><h2>{esc(selected_record.get("name", "Account record"))}</h2><div class="meta">Read-only CRM snapshot · {source_link}</div><div class="table-wrap"><table class="facility-table"><tbody>{record_rows}</tbody></table></div></div><div class="card"><h2>Related decisions</h2>{decision_section}</div>'''
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Bellhaven Review</title><style>{CSS}</style></head>
    <body><header><h1>Bellhaven Ownership Review</h1><div>Human approval required before every CRM write</div></header><main>{notice}
    <section class="stats"><a class="stat" href="/?view=facilities"><b>{summary.get('website_facilities','–')}</b>website facilities</a><a class="stat" href="/?view=pending"><b>{len(pending)}</b>pending</a><a class="stat" href="/?view=history&amp;status=approved"><b>{approved}</b>approved</a><a class="stat" href="/?view=history&amp;status=rejected"><b>{rejected}</b>rejected</a><a class="stat" href="/?view=exceptions&amp;filter=open"><b>{flagged_count}</b>flagged records</a></section>
    <div class="toolbar"><form method="post" action="/refresh"><button>Refresh analysis</button></form><a class="button secondary" href="/">Home</a><a class="button secondary" href="/?view=history">Decision history</a><span class="meta">Refresh is read-only. Decisions are retained.</span></div>
    {(f'<div class="card"><h2>{history_heading}</h2>' + history_body + '</div>') if view == 'history' else facilities_html if view == 'facilities' else pending_html if view == 'pending' else absent_html if view == 'exceptions' else account_html if view == 'account' else classification_html + watchlist_summary}</main></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def send_page(self, body: str, status: int = 200):
        encoded = body.encode()
        self.send_response(status); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded))); self.end_headers(); self.wfile.write(encoded)

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        view = query.get("view", ["dashboard"])[0]
        decision_filter = query.get("status", [""])[0]
        sort_order = query.get("order", ["newest"])[0]
        exception_filter = query.get("filter", ["open"])[0]
        exception_sort = query.get("sort", ["name"])[0]
        exception_order = query.get("direction", ["asc"])[0]
        self.send_page(page(
            view=view if view in {"dashboard", "pending", "history", "facilities", "exceptions", "account"} else "dashboard",
            decision_filter=decision_filter if decision_filter in {"approved", "rejected"} else "",
            sort_order=sort_order if sort_order in {"newest", "oldest"} else "newest",
            account_id=query.get("id", [""])[0],
            exception_filter=exception_filter if exception_filter in {"open", "resolved", "all", "website_absent", "duplicate", "chow"} else "open",
            exception_sort=exception_sort if exception_sort in {"name", "status", "type", "reason", "decision"} else "name",
            exception_order=exception_order if exception_order in {"asc", "desc"} else "asc",
        ))

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            form = parse_qs(self.rfile.read(length).decode())
            if self.path == "/refresh":
                run_pipeline(ApiClient())
                self.send_page(page("Analysis refreshed; no CRM changes were made.")); return
            if self.path != "/decide":
                self.send_page(page("Unknown action"), 404); return
            proposal_id = form.get("id", [""])[0]
            decision = form.get("decision", [""])[0]
            comment = form.get("comment", [""])[0].strip()[:500]
            data = load_json(PROPOSALS_PATH, {"proposals": []})
            proposal = next((p for p in data["proposals"] if p["id"] == proposal_id), None)
            if not proposal or decision not in {"approved", "rejected"}:
                raise ValueError("Invalid proposal or decision")
            decisions = load_json(DECISIONS_PATH, {})
            if proposal_id in decisions:
                self.send_page(page("This proposal was already decided; no action repeated.")); return
            record = {
                "status": decision,
                "decided_at": datetime.now(timezone.utc).isoformat(),
                "proposal": proposal,
                "reviewer": os.getenv("BELLHAVEN_REVIEWER_NAME", "Local reviewer"),
            }
            if comment:
                record["comment"] = comment
            if decision == "approved":
                record["result"] = apply_proposal(ApiClient(), proposal)
            decisions[proposal_id] = record
            save_json(DECISIONS_PATH, decisions)
            proposal["decision"] = record
            save_json(PROPOSALS_PATH, data)
            self.send_page(page(f"Proposal {decision}.", view="pending"))
        except Exception as exc:
            self.send_page(f'<html><style>{CSS}</style><main><div class="error">{esc(exc)}</div><p><a href="/">Return home</a></p></main></html>', 500)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    run_pipeline(ApiClient())
    print("Review app: http://127.0.0.1:8000")
    ThreadingHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
