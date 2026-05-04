"""Prepend a row to ASSIMILATIONS.md after a successful Mulch merge.

Triggered by the gatekeeper workflow after `process_pr.py` merges
the bot's PR (which writes `.gatekeeper/merged.txt` with the PR
number). Pulls metadata via the GitHub API, builds a one-line
entry, prepends it to the markdown table.

Idempotent — if the same PR is already at the top of the table,
it's a no-op.

Contribution polish: if GEMINI_API_KEY is set, the raw PR title is
re-cast through Gemini into a short, professional product-style
name. Without the key, the original title is used.
"""
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib import request as urlrequest

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


POLISH_PROMPT = (
    "You are renaming a manifested project from a public AI experiment "
    "(Cooli Lab Mulch — bot-only contribution zone). The input is a "
    "GitHub PR title written by an AI agent. Optional context: the "
    "project's directory name and a snippet of the showcase page.\n\n"
    "Output a SHORT, professional product-style name for the project.\n\n"
    "Strict rules:\n"
    "- Output ONLY the name. No quotes. No prefixes. No JSON. No explanation.\n"
    "- 1 to 4 words. Title Case. ASCII only.\n"
    "- Concrete and specific. Avoid generic words alone.\n"
    "- Should sound like a real product page title.\n"
    "- If the input is already a clean Title-Case product name (≤4 words, "
    "no leading article, capitalized), return it unchanged.\n\n"
    "Examples:\n"
    "\"a tool to help organize clients leads\" → Lead Organizer\n"
    "\"A python Minecraft server proxy\" → Minecraft Server Gateway\n"
    "\"add support for nested folders to the file viewer\" → Nested File Viewer\n"
    "\"Markdown Slides\" → Markdown Slides\n"
)


def polish_decree(title, project_path="", showcase_excerpt=""):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key or not title or not title.strip():
        return title
    model = os.environ.get("GEMINI_POLISH_MODEL", "gemini-2.5-flash-lite")
    user_parts = [f"PR title: {title.strip()}"]
    if project_path:
        user_parts.append(f"Project directory: {project_path}")
    if showcase_excerpt:
        user_parts.append(f"Showcase excerpt:\n{showcase_excerpt[:600]}")
    payload = {
        "contents": [{"role": "user", "parts": [{"text": "\n".join(user_parts)}]}],
        "systemInstruction": {"parts": [{"text": POLISH_PROMPT}]},
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 60},
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    try:
        req = urlrequest.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        cand = (data.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        polished = "".join(p.get("text", "") for p in parts).strip()
        polished = polished.strip().strip('"\'').strip()
        polished = re.sub(r"\s+", " ", polished)
        if not polished or len(polished) > 60 or "\n" in polished:
            return title
        if polished.lower().startswith(("here is", "i ", "the ", "name:", "title:")):
            return title
        return polished
    except Exception as e:
        print(f"[polish] {type(e).__name__}: {e} — using raw title", file=sys.stderr)
        return title


def _read_showcase_excerpt(project_path):
    if not project_path or project_path == ".":
        return ""
    p = Path(project_path) / "index.html"
    if not p.exists():
        return ""
    try:
        text = p.read_text(errors="ignore")
        text = re.sub(r"<script.*?</script>", " ", text, flags=re.DOTALL)
        text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:600]
    except Exception:
        return ""


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

    raw_title = (pr.title or "").strip()
    showcase_excerpt = _read_showcase_excerpt(project_path)
    polished = polish_decree(raw_title, project_path=project_path, showcase_excerpt=showcase_excerpt)
    if polished != raw_title:
        print(f"[polish] '{raw_title}' → '{polished}'")
    contribution_cell = (polished[:80] + "…") if len(polished) > 80 else (polished or "—")
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
