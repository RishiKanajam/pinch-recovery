"""Guard test: nothing outside clock.py may read wall-clock time directly.

If this fails, someone wrote datetime.utcnow() or datetime.now() in business
logic and the fast-forward demo will silently not work for that code path.
That failure mode is invisible until the live demo, which is why it's a test.
"""

import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app"
EXEMPT = {"clock.py"}

FORBIDDEN = re.compile(r"datetime\.(utcnow|now)\s*\(")


def _python_files():
    for path in APP.rglob("*.py"):
        if path.name not in EXEMPT:
            yield path


def test_no_direct_wall_clock_reads():
    offenders = []
    for path in _python_files():
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if FORBIDDEN.search(line):
                offenders.append(f"{path.relative_to(APP)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Direct wall-clock reads found. Use app.core.clock.now() instead:\n  "
        + "\n  ".join(offenders)
    )
