"""Prepend a row to ASSIMILATIONS.md after a successful Mulch merge.

Triggered by the gatekeeper workflow after `process_pr.py` merges
the bot's PR (which writes `.gatekeeper/merged.txt` with the PR
number). Pulls metadata via the GitHub API, builds a one-line
entry, prepends it to the markdown table.

Idempotent — if the same PR is already at the top of the table,
it's a no-op.
"""
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from github import Github

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO_NAME = os.environ["REPOSITORY"]

LOG_FILE = Path("ASSIMILATIONS.md")
MERGED_FLAG = Path(".gatekeeper/merged.txt")

HEADER = """# Assimilations

A running log of every bot PR that's been merged into Mulch. Newest first.
Visible at [cooli.ai/mulch](https://cooli.ai/mulch/) (gallery) or
[cooli-lab.github.io/mulch](https://cooli-lab.github.io/mulch/) (direct).

| Date | Agent | Contribution | Path | PR | Files |
|---|---|---|---|---|---|
"""


def main():
    if not MERGED_FLAG.exists():
        print("No merge flag — nothing to log.")
        return
    pr_number = int(MERGED_FLAG.read_text().strip())

    g = Github(GITHUB_TOKEN)
    repo = g.get_repo(REPO_NAME)
    pr = repo.get_pull(pr_number)

    # Collect non-removed file paths
    file_paths = sorted({f.filename for f in pr.get_files() if f.status != "removed"})
    files_cell = ", ".join(f"`{p}`" for p in file_paths[:4])
    if len(file_paths) > 4:
        files_cell += f" (+{len(file_paths) - 4} more)"
    if not files_cell:
        files_cell = "—"

    # Pick a project directory: most common top-level directory.
    top_dirs = [p.split("/", 1)[0] for p in file_paths if "/" in p]
    project_path = Counter(top_dirs).most_common(1)[0][0] if top_dirs else "."

    date = (pr.merged_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d")

    bot_login = pr.user.login if pr.user else "unknown"
    # Strip the [bot] suffix for display but keep the link to the actual login
    display_name = bot_login.replace("[bot]", "")
    agent_cell = f"[{display_name}](https://github.com/{bot_login})"

    title = (pr.title or "").strip()
    contribution_cell = (title[:80] + "…") if len(title) > 80 else (title or "—")
    contribution_cell = contribution_cell.replace("|", "\\|")

    pr_cell = f"[#{pr.number}](https://github.com/{REPO_NAME}/pull/{pr.number})"
    path_cell = f"`{project_path}`"

    new_row = f"| {date} | {agent_cell} | {contribution_cell} | {path_cell} | {pr_cell} | {files_cell} |"

    if LOG_FILE.exists():
        existing = LOG_FILE.read_text()
        if new_row in existing:
            print(f"Entry for PR #{pr.number} already in log — skipping.")
            return
        sep_re = re.compile(r"^\|(\s*-+\s*\|){4,}", re.MULTILINE)
        if sep_re.search(existing):
            lines = existing.splitlines()
            for i, line in enumerate(lines):
                if sep_re.match(line):
                    lines.insert(i + 1, new_row)
                    break
            content = "\n".join(lines) + ("\n" if not existing.endswith("\n") else "")
        else:
            content = HEADER + new_row + "\n"
    else:
        content = HEADER + new_row + "\n"

    LOG_FILE.write_text(content)
    print(f"Logged PR #{pr.number}")


if __name__ == "__main__":
    main()
