"""Card art lookup via pokemontcg.io (the plan's card-metadata source).

Used by `python -m src.manage fetch-images` to find binder art for cards that
were added without an image URL. Not part of the decision loop. The lookup and
caching live in src/pokemontcg.py, shared with the free price source; card
art never changes, so it's cached forever.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .pokemontcg import find_card


def find_image_url(name: str, session=None, sleep: Callable[[float], None] = time.sleep) -> str | None:
    """Return a small card-image URL for a name like 'Mew ex 232/091', or None."""
    card = find_card(name, select="id,name,images", ttl=None, session=session, sleep=sleep)
    return (card or {}).get("images", {}).get("small")
