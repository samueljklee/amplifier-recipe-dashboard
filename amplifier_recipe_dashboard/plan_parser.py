"""Parse implementation plan files to extract task lists.

Supports two header formats observed in the wild:
    ## Task N: Description   (older, phase plans)
    ### Task N: Description  (newer, batch plans)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Match both ## and ### task headers
_TASK_HEADER_RE = re.compile(
    r"^#{2,3}\s+Task\s+(\d+)[:\s]\s*(.+)$",
    re.IGNORECASE,
)


@dataclass
class PlanTask:
    """A task extracted from a plan file."""

    number: int
    description: str
    line_number: int


def parse_plan(plan_path: str | Path) -> list[PlanTask]:
    """Extract tasks from a plan file.

    Returns a list of PlanTask sorted by task number.
    """
    path = Path(plan_path)
    if not path.is_file():
        return []

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    tasks: list[PlanTask] = []
    for i, line in enumerate(lines, start=1):
        m = _TASK_HEADER_RE.match(line.strip())
        if m:
            task_num = int(m.group(1))
            description = m.group(2).strip()
            tasks.append(PlanTask(number=task_num, description=description, line_number=i))

    tasks.sort(key=lambda t: t.number)
    return tasks
