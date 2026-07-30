#!/usr/bin/env python3
"""
Weekly Stuck-Ticket Digest — Jira -> Slack

Pulls all open-sprint tickets for a Jira project, applies stall-detection
rules (sprint carryover, story-point-scaled time-in-status, blocked/clarify
staleness), and posts a formatted digest to a Slack channel.

Run manually:
    python3 digest.py

Run on a schedule via cron (every Monday 9am):
    0 9 * * 1 /usr/bin/python3 /path/to/digest.py >> /path/to/digest.log 2>&1

Or via GitHub Actions (see .github/workflows/weekly-digest.yml).

Required environment variables (put these in a .env file or your
scheduler's secrets store — never commit real values):
    JIRA_BASE_URL       e.g. https://zuub-team.atlassian.net
    JIRA_EMAIL          the email of the account that owns the API token
    JIRA_API_TOKEN      https://id.atlassian.com/manage-profile/security/api-tokens
    JIRA_PROJECT_KEY    e.g. BOT
    SLACK_BOT_TOKEN     xoxb-... (needs chat:write scope)
    SLACK_CHANNEL_ID    e.g. C0BMPDU6272 (use #testingjira while testing,
                         switch to the #zuub-team channel ID when ready)
"""

import os
import sys
import json
from datetime import datetime, timezone
from collections import defaultdict

import requests

# ---------------------------------------------------------------------------
# Config — thresholds agreed with the team. Adjust here as norms change.
# ---------------------------------------------------------------------------

# Sprint-carryover: how many sprints a ticket can be dragged across before
# it's always flagged, regardless of current status.
CARRYOVER_THRESHOLD = {
    "High": 2,
    "Highest": 2,
    "Medium": 3,
    "Low": 3,
    "Lowest": 3,
}
DEFAULT_CARRYOVER_THRESHOLD = 3

# Active-work statuses where time-in-status is scaled by story points.
ACTIVE_STATUSES = {
    "Dev In Progress",
    "Code Review",
    "Testing To Do",
    "Testing In Progress",
}

# Story-point-scaled day thresholds for ACTIVE_STATUSES.
def days_threshold_for_points(points):
    if points is None:
        points = 3  # assume medium if unestimated
    if points <= 2:
        return 3
    elif points <= 5:
        return 5
    else:
        return 8

# Statuses that need a human keep/defer/backlog decision, not just a nudge.
ACTION_REQUIRED_STATUSES = {"Blocked", "Clarify", "On Hold"}
ACTION_REQUIRED_MIN_DAYS = 3  # don't flag same-day entries

# "Dev To Do" is a To-Do bucket but flagged same as carryover if it repeats
# across multiple sprints (ticket keeps getting bumped before dev starts).
TODO_CARRYOVER_STATUS = "Dev To Do"

PRIORITY_SORT_ORDER = {"Highest": 0, "High": 1, "Medium": 2, "Low": 3, "Lowest": 4}


# ---------------------------------------------------------------------------
# Jira API helpers
# ---------------------------------------------------------------------------

def jira_session():
    base_url = os.environ["JIRA_BASE_URL"].rstrip("/")
    email = os.environ["JIRA_EMAIL"]
    token = os.environ["JIRA_API_TOKEN"]
    s = requests.Session()
    s.auth = (email, token)
    s.headers.update({"Accept": "application/json"})
    return s, base_url


def fetch_open_sprint_issues(session, base_url, project_key):
    """Fetch all non-Done issues in the currently open sprint(s)."""
    jql = (
        f'project = {project_key} AND sprint in openSprints() '
        f'AND statusCategory != Done ORDER BY priority ASC'
    )
    issues = []
    next_page_token = None
    fields = ["summary", "status", "assignee", "priority", "issuetype",
              "customfield_10020", "customfield_10064"]  # Sprint, Story Points
    # NOTE: customfield IDs for Sprint / Story Points are workspace-specific.
    # Confirm these against your instance (we found 10020 / 10064 for Zuub's
    # Jira via the changelog field IDs) or fetch dynamically via
    # /rest/api/3/field if you're pointing this at a different site.

    while True:
        params = {
            "jql": jql,
            "fields": ",".join(fields),
            "maxResults": 100,
        }
        if next_page_token:
            params["nextPageToken"] = next_page_token
        resp = session.get(f"{base_url}/rest/api/3/search/jql", params=params)
        resp.raise_for_status()
        data = resp.json()
        issues.extend(data.get("issues", []))
        next_page_token = data.get("nextPageToken")
        if data.get("isLast", True) or not next_page_token:
            break

    return issues


def fetch_changelog(session, base_url, issue_key):
    """Fetch full changelog for a single issue (status + sprint history)."""
    resp = session.get(
        f"{base_url}/rest/api/3/issue/{issue_key}",
        params={"expand": "changelog", "fields": "status"},
    )
    resp.raise_for_status()
    return resp.json().get("changelog", {}).get("histories", [])


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def parse_jira_ts(ts):
    # Jira timestamps look like 2026-07-15T10:32:01.091-0700
    return datetime.strptime(ts[:19] + ts[19:22] + ts[23:], "%Y-%m-%dT%H:%M:%S%z")


def analyze_issue(issue, histories, now):
    """Compute the signals we need from an issue's changelog."""
    key = issue["key"]
    fields = issue["fields"]
    summary = fields["summary"]
    status_name = fields["status"]["name"]
    priority = fields.get("priority", {}).get("name", "Medium")
    assignee = fields.get("assignee")
    assignee_name = assignee["displayName"] if assignee else "Unassigned"
    story_points = fields.get("customfield_10064")
    created = parse_jira_ts(issue["fields"].get("created")) if issue["fields"].get("created") else None

    # Sort histories chronologically (Jira returns newest-first sometimes,
    # oldest-first other times depending on endpoint — sort to be safe).
    histories_sorted = sorted(histories, key=lambda h: h["created"])

    # --- current status entry timestamp ---
    status_entered_at = created
    for h in histories_sorted:
        for item in h.get("items", []):
            if item.get("field") == "status" and item.get("toString") == status_name:
                status_entered_at = parse_jira_ts(h["created"])
    days_in_status = (now - status_entered_at).days if status_entered_at else 0

    # --- sprint carryover count: distinct sprint IDs ever attached ---
    max_sprint_count = 0
    for h in histories_sorted:
        for item in h.get("items", []):
            if item.get("field") == "Sprint" and item.get("to"):
                count = len([s for s in item["to"].split(",") if s.strip()])
                max_sprint_count = max(max_sprint_count, count)

    return {
        "key": key,
        "summary": summary,
        "status": status_name,
        "priority": priority,
        "assignee": assignee_name,
        "story_points": story_points,
        "days_in_status": days_in_status,
        "sprint_carryover": max_sprint_count,
    }


def classify(analyzed):
    """Return ('stuck', reason_str) or ('action_required', reason_str) or None."""
    carryover_limit = CARRYOVER_THRESHOLD.get(analyzed["priority"], DEFAULT_CARRYOVER_THRESHOLD)

    # Rule 1 + 2: sprint carryover (any status) or Dev To Do carryover
    if analyzed["sprint_carryover"] >= carryover_limit:
        return "stuck", f"Carried {analyzed['sprint_carryover']} sprints"

    # Rule 3: active-work statuses, story-point scaled
    if analyzed["status"] in ACTIVE_STATUSES:
        limit = days_threshold_for_points(analyzed["story_points"])
        if analyzed["days_in_status"] > limit:
            return "stuck", (
                f"{analyzed['status']}, {analyzed['days_in_status']} days "
                f"(limit {limit}d for {analyzed['story_points'] or '?'} pts)"
            )

    # Rule 4: blocked/clarify/on hold -> action required, once stale
    if analyzed["status"] in ACTION_REQUIRED_STATUSES:
        if analyzed["days_in_status"] >= ACTION_REQUIRED_MIN_DAYS:
            return "action_required", f"{analyzed['status']}, {analyzed['days_in_status']} days"

    return None, None


# ---------------------------------------------------------------------------
# Slack formatting + posting
# ---------------------------------------------------------------------------

def issue_url(base_url, key):
    return f"{base_url}/browse/{key}"


def format_line(base_url, analyzed, reason):
    link = f"<{issue_url(base_url, analyzed['key'])}|{analyzed['key']}>"
    return (
        f"{link} — {analyzed['priority']} — {analyzed['summary']}\n"
        f"{reason}\n"
        f"Assignee: {analyzed['assignee']}\n"
    )


def build_digest_message(base_url, sprint_label, stuck, action_required):
    stuck_sorted = sorted(stuck, key=lambda x: PRIORITY_SORT_ORDER.get(x[0]["priority"], 99))
    action_sorted = sorted(action_required, key=lambda x: PRIORITY_SORT_ORDER.get(x[0]["priority"], 99))

    lines = [f"📋 *Weekly Sprint Health Digest — {sprint_label}*\n"]

    if stuck_sorted:
        lines.append("*🔴 Stuck Tickets (sorted by priority)*\n")
        for analyzed, reason in stuck_sorted:
            lines.append(format_line(base_url, analyzed, reason))
    else:
        lines.append("*🔴 Stuck Tickets*\nNone this week 🎉\n")

    if action_sorted:
        lines.append("━━━━━━━━━━━━━━━━━━\n")
        lines.append("*⚠️ Action Required — needs a keep/defer/backlog decision*\n")
        for analyzed, reason in action_sorted:
            lines.append(format_line(base_url, analyzed, reason))

    return "\n".join(lines)


def post_to_slack(message):
    token = os.environ["SLACK_BOT_TOKEN"]
    channel = os.environ["SLACK_CHANNEL_ID"]
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {token}"},
        json={"channel": channel, "text": message},
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack API error: {data}")
    return data


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    project_key = os.environ.get("JIRA_PROJECT_KEY", "BOT")
    now = datetime.now(timezone.utc)

    session, base_url = jira_session()

    print(f"Fetching open-sprint issues for {project_key}...")
    issues = fetch_open_sprint_issues(session, base_url, project_key)
    print(f"Found {len(issues)} issues in open sprint(s).")

    stuck, action_required = [], []
    sprint_label = "current sprint"

    for issue in issues:
        key = issue["key"]
        histories = fetch_changelog(session, base_url, key)
        analyzed = analyze_issue(issue, histories, now)
        category, reason = classify(analyzed)
        if category == "stuck":
            stuck.append((analyzed, reason))
        elif category == "action_required":
            action_required.append((analyzed, reason))

    print(f"{len(stuck)} stuck, {len(action_required)} action-required.")

    message = build_digest_message(base_url, sprint_label, stuck, action_required)

    if "--dry-run" in sys.argv:
        print("\n--- DRY RUN: message not sent ---\n")
        print(message)
    else:
        result = post_to_slack(message)
        print(f"Posted to Slack: {result.get('ts')}")


if __name__ == "__main__":
    main()
