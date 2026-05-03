"""Post the verdict comment and close the PR.

If `.gatekeeper/roast.txt` exists (Claude wrote one), it becomes the
comment body — a 1–2 line roast of the rejected request. Otherwise the
bare reason from `process_pr.py` is used as a graceful fallback (when
no Claude token is configured, when the action errors, etc.).

If no `.gatekeeper/violation.txt` exists, this script is a no-op — the
PR was merged cleanly by `process_pr.py` and there's nothing to seal.
"""
import os
import sys
from pathlib import Path

from github import Github

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO_NAME = os.environ["REPOSITORY"]

GATE = Path(".gatekeeper")


def main():
    violation_path = GATE / "violation.txt"
    if not violation_path.exists():
        print("No violation to seal — clean merge.")
        return

    violation = violation_path.read_text().strip()
    pr_number_text = (GATE / "pr_number.txt").read_text().strip()
    pr_number = int(pr_number_text)

    roast_path = GATE / "roast.txt"
    roast = roast_path.read_text().strip() if roast_path.exists() else ""

    if roast:
        body = f"🛑 *Directive Violation.*\n\n{roast}"
    else:
        body = violation  # fall back to the bare reason

    g = Github(GITHUB_TOKEN)
    repo = g.get_repo(REPO_NAME)
    pr = repo.get_pull(pr_number)
    pr.create_issue_comment(body)
    pr.edit(state="closed")
    print(f"Sealed PR #{pr_number}")


if __name__ == "__main__":
    main()
