"""Parsing helpers for card names like 'Charizard ex 199/165'.

Shared by the live market source, the image lookup and the web UI, so it lives
in its own tiny module rather than inside any one of them.
"""

from __future__ import annotations

import re

_NUMBER_RE = re.compile(r"\b(\d{1,3})/(\d{1,3})\b")


def card_number(name: str) -> str | None:
    """Collector number from a name: 'Charizard ex 199/165' -> '199/165'."""
    m = _NUMBER_RE.search(name)
    return m.group(0) if m else None


def base_name(name: str) -> str:
    """Name without the collector number: 'Charizard ex 199/165' -> 'Charizard ex'."""
    return _NUMBER_RE.sub("", name).strip()


def number_parts(name: str) -> tuple[int, int] | None:
    """('020/189') -> (20, 189): collector number and the set's printed total."""
    m = _NUMBER_RE.search(name)
    return (int(m.group(1)), int(m.group(2))) if m else None
