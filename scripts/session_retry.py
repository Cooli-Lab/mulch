"""Re-run failed workflow runs once Claude's session limit has likely reset.

Claude Max sessions reset on a 5-hour rolling window. When the Genesis
Handler hits that limit, the run fails partway through. We don't try to
distinguish session-limit failures from genuine bugs in this script —
instead we apply a simple heuristic: any failed `issues`-triggered run
that's been failed for 4–16 hours and hasn't been retried yet gets one
automatic re-run. `gh run rerun --failed` preserves the original event
payload, so the workflow re-fires with the same issue context.

Genuine bugs (not session-limit) will fail again on retry; the
MAX_ATTEMPTS cap (2) prevents infinite loops.

Invoked by `.github/workflows/session-retry.yml`. Reads $GH_TOKEN.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

WORKFLOW = os.environ.get("WORKFLOW_NAME", "Autonomous Gatekeeper")
RETRY_AFTER_H = float(os.environ.get("RETRY_AFTER_H", "4"))
RETRY_BEFORE_H = float(os.environ.get("RETRY_BEFORE_H", "16"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "2"))


def gh(*args):
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"gh {' '.join(args)} failed: {r.stderr.strip()}", file=sys.stderr)
        return ""
    return r.stdout


def main():
    raw = gh(
        "run", "list",
        "--workflow", WORKFLOW,
        "--status", "failure",
        "--limit", "50",
        "--json", "databaseId,createdAt,attempt,event,conclusion,displayTitle",
    )
    if not raw.strip():
        print("No failed runs found.")
        return
    runs = json.loads(raw)
    now = datetime.now(timezone.utc)
    retried = 0
    skipped = 0
    for r in runs:
        if r.get("event") not in ("issues", "pull_request_target", "repository_dispatch", "workflow_dispatch"):
            skipped += 1
            continue
        if int(r.get("attempt", 0)) >= MAX_ATTEMPTS:
            skipped += 1
            continue
        created = datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00"))
        age_h = (now - created).total_seconds() / 3600
        if age_h < RETRY_AFTER_H or age_h > RETRY_BEFORE_H:
            skipped += 1
            continue
        rid = r["databaseId"]
        title = r.get("displayTitle", "")[:60]
        print(f"Re-running #{rid} ({age_h:.1f}h old, attempt {r['attempt']}): {title}")
        out = gh("run", "rerun", "--failed", str(rid))
        if out:
            print(f"  → {out.strip()}")
        retried += 1
    print(f"\nDone. Retried {retried}, skipped {skipped}.")


if __name__ == "__main__":
    main()
