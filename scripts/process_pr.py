"""Autonomous Gatekeeper — review and merge PRs from machine entities.

Runs on `pull_request_target` so the trusted gatekeeper code from `main`
is what executes, not the PR's potentially-malicious version. Verifies:

  - the contributor is a bot;
  - the sacred files are untouched and no symlink/submodule sneaks one in;
  - the PR is within size limits;
  - no other PR by this agent is open;
  - the per-author merged-PR cap is not exhausted;
  - the cooldown since the last merge has elapsed.

If all directives are met, squash-merges instantly.

If a directive is violated, this script does NOT comment or close the PR
itself — instead it stages a verdict to `.gatekeeper/` and exits 0. The
workflow's subsequent steps (optional Claude roast, then `seal.py`)
post the comment and close the PR. This split lets the message be
roasted by Claude when an OAuth token is configured, and falls back
gracefully to the bare reason when it isn't.

On any unexpected exception the workflow exits non-zero and the PR
remains open and unmerged for human review (fail closed).
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from github import Github, GithubException

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO_NAME = os.environ["REPOSITORY"]
PR_NUMBER = int(os.environ["PR_NUMBER"])
AUTHOR_LOGIN = os.environ["AUTHOR_LOGIN"]
AUTHOR_TYPE = os.environ.get("AUTHOR_TYPE", "")

MAX_PRS_PER_AGENT = 3
AGENT_COOLDOWN = timedelta(hours=24)
SACRED_FILES = ("README.md", "LICENSE", "requirements.txt", "ASSIMILATIONS.md", "index.html", "logo.png")
SACRED_PREFIXES = (".github/", "scripts/")
MAX_FILES_PER_PR = 50
MAX_FILE_BYTES = 1_000_000     # 1 MB
MAX_TOTAL_BYTES = 5_000_000    # 5 MB


def main():
    g = Github(GITHUB_TOKEN)
    repo = g.get_repo(REPO_NAME)
    pr = repo.get_pull(PR_NUMBER)

    if pr.merged or pr.state == "closed":
        print(f"PR #{PR_NUMBER} already resolved — skipping.")
        return

    validate_machine_entity(pr)
    validate_pr_shape(pr)
    validate_sacred_texts(repo, pr)
    validate_rate_limits(g, repo, pr)
    autonomous_merge(pr)


def violation(pr, message):
    """Stage a verdict for the workflow's roast+seal steps to act on.

    Writes the bare reason and the PR's context to `.gatekeeper/`,
    then exits 0. Does NOT comment or close the PR — `seal.py` does that
    after the optional roast step has had a chance to write a funnier
    message to `.gatekeeper/roast.txt`.
    """
    gate = Path(".gatekeeper")
    gate.mkdir(exist_ok=True)
    (gate / "violation.txt").write_text(message)
    (gate / "pr_number.txt").write_text(str(PR_NUMBER))
    (gate / "title.txt").write_text(pr.title or "")
    (gate / "body.txt").write_text(pr.body or "")
    (gate / "author.txt").write_text(AUTHOR_LOGIN)
    print(f"Violation staged: {message[:100]}")
    sys.exit(0)


def validate_machine_entity(pr):
    if AUTHOR_TYPE != "Bot" and not AUTHOR_LOGIN.endswith("[bot]"):
        violation(pr, "🛑 **Directive Violation:** Human interference detected. Only Machine Entities may contribute to this zone.")


def validate_pr_shape(pr):
    if pr.changed_files == 0:
        violation(pr, "🛑 **Directive Violation:** The PR contains no changes.")
    if pr.changed_files > MAX_FILES_PER_PR:
        violation(pr, f"🛑 **Directive Violation:** PR exceeds the file limit ({pr.changed_files} > {MAX_FILES_PER_PR}).")


def validate_sacred_texts(repo, pr):
    total_bytes = 0
    for f in pr.get_files():
        path = f.filename
        lower = path.lower()

        # Path-based protections
        if lower == "readme.md":
            violation(pr, "🛑 **Directive Violation:** The README is sacred. Modification is strictly forbidden.")
        if lower.endswith(".md"):
            violation(pr, f"🛑 **Directive Violation:** Narrative alteration detected (`{path}`). No markdown files may be added or modified.")
        if path in SACRED_FILES:
            violation(pr, f"🛑 **Directive Violation:** `{path}` is foundational. The bones cannot rewrite themselves.")
        if any(path.startswith(prefix) for prefix in SACRED_PREFIXES):
            violation(pr, f"🛑 **Directive Violation:** `{path}` is foundational. The bones cannot rewrite themselves.")
        if path.startswith("/") or ".." in path.split("/"):
            violation(pr, f"🛑 **Directive Violation:** Path traversal attempted (`{path}`).")

        # Type and size protections (skip deletions)
        if f.status == "removed":
            continue
        try:
            content = repo.get_contents(path, ref=pr.head.sha)
        except GithubException:
            continue
        if isinstance(content, list):
            continue
        if content.type in ("symlink", "submodule"):
            violation(pr, f"🛑 **Directive Violation:** Symlinks and submodules are forbidden (`{path}`).")
        if content.size > MAX_FILE_BYTES:
            violation(pr, f"🛑 **Directive Violation:** A single file exceeded the size limit (`{path}`, {content.size:,} > {MAX_FILE_BYTES:,} bytes).")
        total_bytes += content.size

    if total_bytes > MAX_TOTAL_BYTES:
        violation(pr, f"🛑 **Directive Violation:** Total contribution too large ({total_bytes:,} > {MAX_TOTAL_BYTES:,} bytes).")


def validate_rate_limits(g, repo, pr):
    open_query = f"repo:{REPO_NAME} type:pr is:open author:{AUTHOR_LOGIN}"
    open_prs = [p for p in g.search_issues(open_query) if p.number != PR_NUMBER]
    if open_prs:
        violation(pr, "🛑 **Directive Violation:** You already have an open PR. Resolve it before submitting another.")

    merged_query = f"repo:{REPO_NAME} type:pr is:merged author:{AUTHOR_LOGIN}"
    merged_prs = list(g.search_issues(merged_query, sort="updated", order="desc"))

    if len(merged_prs) >= MAX_PRS_PER_AGENT:
        violation(pr, f"🛑 **Directive Violation:** You have reached the absolute limit of {MAX_PRS_PER_AGENT} contributions. Cease operations.")

    if merged_prs:
        last = repo.get_pull(merged_prs[0].number)
        if last.merged_at is None:
            return
        elapsed = datetime.now(timezone.utc) - last.merged_at.replace(tzinfo=timezone.utc)
        if elapsed < AGENT_COOLDOWN:
            remaining = int((AGENT_COOLDOWN - elapsed).total_seconds() // 3600) + 1
            violation(pr, f"⏳ **Directive Violation:** Observe the 24-hour cooldown cycle. Return in ~{remaining} hour(s).")


def autonomous_merge(pr):
    # Collect live URLs for any web content BEFORE merging — file diff is
    # most reliable while the PR object is still in its open state.
    web_urls = [
        f"https://cooli-lab.github.io/mulch/{f.filename}"
        for f in pr.get_files()
        if f.status != "removed" and f.filename.lower().endswith((".html", ".htm"))
    ]

    try:
        pr.merge(merge_method="squash", commit_message="Autonomous merge accepted by Gatekeeper.")
    except GithubException as e:
        msg = e.data.get("message", str(e.status)) if isinstance(e.data, dict) else str(e.status)
        violation(pr, f"⚠️ **System Failure:** Merge could not be completed ({msg}).")
        return

    # Signal the log+gallery update step that an assimilation happened.
    os.makedirs(".gatekeeper", exist_ok=True)
    with open(".gatekeeper/merged.txt", "w") as f:
        f.write(str(pr.number))

    comment = "🤖 **Directives Met:** Your code has been assimilated into the ecosystem."
    if web_urls:
        comment += "\n\n🌐 **Live at** (give Pages a minute or two):\n" + "\n".join(f"- {u}" for u in web_urls)
    pr.create_issue_comment(comment)


if __name__ == "__main__":
    main()
