"""ULID-style identifiers matching the `pay_01HX...` shape in docs/CONTRACT.md.

Hand-rolled rather than pulled from a library: requirements.txt is pinned and
frozen for the hackathon, and this is ~20 lines.

The timestamp half comes from clock.now(), not the wall clock, so ids stay
sortable in the same order events actually happened during a fast-forward.
"""

from __future__ import annotations

import os

from app.core import clock

# Crockford base32 — no I, L, O, or U, so an id read aloud off a screen during
# a demo is unambiguous.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_TIME_CHARS = 10  # 48-bit millisecond timestamp
_RANDOM_CHARS = 16  # 80 bits of randomness


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        value, remainder = divmod(value, 32)
        out.append(_ALPHABET[remainder])
    return "".join(reversed(out))


def ulid() -> str:
    """A 26-character ULID: sortable by time, random within the millisecond."""
    milliseconds = int(clock.now().timestamp() * 1000)
    randomness = int.from_bytes(os.urandom(10), "big")
    return _encode(milliseconds, _TIME_CHARS) + _encode(randomness, _RANDOM_CHARS)


def new_id(prefix: str) -> str:
    """Prefixed id, e.g. new_id("pay") -> "pay_01K9...".

    The prefix makes an id self-describing in a log line or a bug report.
    """
    return f"{prefix}_{ulid()}"
