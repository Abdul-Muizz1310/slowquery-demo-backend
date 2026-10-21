"""Which requests never need a platform token (spec 14, contract v2 §2).

Pure: a method and a path in, a boolean out. Everything not listed here is
protected, including every non-``GET`` request to a commerce path, so a write
can never become public by accident.
"""

from __future__ import annotations

import re
from typing import Final

_PUBLIC_EXACT: Final = frozenset(
    {"/", "/health", "/version", "/metrics", "/openapi.json", "/docs", "/redoc"}
)
_PUBLIC_PREFIXES: Final = ("/docs/", "/redoc/")

# The read-only seeded commerce routes, GET/HEAD only.
_COMMERCE_READ: Final = re.compile(
    r"^/(?:"
    r"users(?:/[^/]+(?:/orders)?)?"
    r"|products(?:/[^/]+)?"
    r"|orders(?:/[^/]+)?"
    r"|order_items"
    r")/?$"
)
_READ_METHODS: Final = frozenset({"GET", "HEAD"})


def is_public(method: str, path: str) -> bool:
    """``True`` when ``method path`` answers without a token."""
    if path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIXES):
        return True
    return method.upper() in _READ_METHODS and _COMMERCE_READ.match(path) is not None
