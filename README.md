# Weekly Stuck Ticket Digest (Jira → Slack)

Posts a weekly digest to Slack listing sprint tickets that appear stuck or
need a keep/defer/backlog decision. Built from the rules worked out with
the team:

- **Sprint carryover**: High priority flagged at 2+ sprints, Medium/Low at 3+
  (checked regardless of current status — this is the strongest signal for
  "got bumped and forgotten").
- **Dev To Do across multiple sprints** counts the same as carryover.
- **Active-work statuses** (Dev In Progress, Code Review, Testing To Do,
  Testing In Progress) are flagged using a story-point-scaled threshold:
  1–2 pts → 3 days, 3–5 pts → 5 days, 8+ pts → 8 days.
- **Blocked / Clarify / On Hold** go to a separate *Action Required* section
  once they've sat for 3+ days — these need a human decision, not a nudge.

## Setup

1. **Jira API token**: create one at
   https://id.atlassian.com/manage-profile/security/api-tokens
2. **Slack bot token**: create a Slack app at https://api.slack.com/apps
   with the `chat:write` scope, install it to your workspace, and invite
   the bot to the target channel (`/invite @your-bot-name`).
3. Copy `.env.example` to `.env` and fill in real values — **do not commit
   `.env`** (add it to `.gitignore`).
4. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
5. Test locally without sending to Slack:
   ```
   set -a; source .env; set +a
   python3 digest.py --dry-run
   ```
6. Once the output looks right, run for real:
   ```
   python3 digest.py
   ```

## Scheduling

**Option A — GitHub Actions (recommended, no server to manage):**
Push this repo to GitHub, then add the six env vars from `.env.example` as
repository secrets (Settings → Secrets and variables → Actions). The
workflow in `.github/workflows/weekly-digest.yml` runs every Monday at
13:00 UTC — adjust the cron expression for your timezone/DST. You can also
trigger it manually from the Actions tab ("Run workflow").

**Option B — cron on a server you control:**
```
0 9 * * 1 cd /path/to/jira-slack-digest && /usr/bin/python3 digest.py >> digest.log 2>&1
```

## Tuning

All thresholds live at the top of `digest.py` — adjust
`CARRYOVER_THRESHOLD`, `ACTIVE_STATUSES`, `days_threshold_for_points()`,
and `ACTION_REQUIRED_MIN_DAYS` as your team's norms evolve.

**Custom field IDs**: `customfield_10020` (Sprint) and `customfield_10064`
(Story Points) were confirmed for the Zuub Jira instance. If you point this
at a different Jira site, verify these via `/rest/api/3/field` first —
custom field IDs are not portable across instances.

## Testing in #testingjira first

Set `SLACK_CHANNEL_ID` to your private test channel while validating
output, then switch it to the real channel's ID once you're happy with the
format and thresholds.
