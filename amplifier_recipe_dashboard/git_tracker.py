"""Match git commits to plan tasks.

Scans git log for commit messages containing task references like:
    task-5, (task-5), Task 5, task 5, (Task 5)
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Match task references in commit messages
_TASK_REF_RE = re.compile(r"task[- ]?(\d+)", re.IGNORECASE)


@dataclass
class GitCommit:
    """A git commit with its parsed task associations."""

    hash: str
    subject: str
    timestamp: str
    task_numbers: list[int]


def get_commits_since(
    repo_path: str | Path,
    since: str | None = None,
    max_count: int = 200,
) -> list[GitCommit]:
    """Get git commits from a repo, optionally since a timestamp.

    Args:
        repo_path: Path to the git repository.
        since: ISO timestamp to start from (e.g., "2026-04-01T00:00:00").
        max_count: Maximum number of commits to return.

    Returns:
        List of GitCommit objects, most recent first.
    """
    path = Path(repo_path)
    if not (path / ".git").is_dir() and not path.name == ".git":
        return []

    cmd = [
        "git",
        "-C",
        str(path),
        "log",
        f"--max-count={max_count}",
        "--format=%h\t%s\t%aI",
    ]
    if since:
        cmd.append(f"--since={since}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=10)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return []

    commits: list[GitCommit] = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        commit_hash, subject, timestamp = parts
        task_nums = [int(m.group(1)) for m in _TASK_REF_RE.finditer(subject)]
        commits.append(
            GitCommit(
                hash=commit_hash,
                subject=subject,
                timestamp=timestamp,
                task_numbers=task_nums,
            )
        )
    return commits


def match_tasks_to_commits(
    commits: list[GitCommit],
    task_count: int,
) -> dict[int, GitCommit]:
    """Map task numbers to their most recent matching commit.

    Returns:
        Dict mapping task_number -> GitCommit for tasks that have matching commits.
    """
    task_commits: dict[int, GitCommit] = {}
    # Iterate oldest-first so most recent commit wins per task
    for commit in reversed(commits):
        for task_num in commit.task_numbers:
            if 1 <= task_num <= task_count:
                task_commits[task_num] = commit
    return task_commits
