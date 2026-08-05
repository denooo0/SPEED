"""Deterministic, content-addressed identity.

Design law: every record in this engine is identified by a function of its
*content*, never by a random UUID or a wall-clock read. This is what makes
replay idempotent -- reprocessing the same input twice produces the same ID,
so the second write is a provable no-op instead of a duplicate.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

_HASH_LEN = 16  # 64 bits of hex; collision-safe at our volumes, compact in paths


def canonical(obj: Any) -> str:
    """Canonical JSON: sorted keys, no whitespace, stable float repr.

    Two structurally-equal objects MUST serialize identically or content
    addressing silently breaks.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_coerce)


def _coerce(o: Any) -> Any:
    if isinstance(o, float):
        return repr(o)
    if hasattr(o, "to_dict"):
        return o.to_dict()
    if hasattr(o, "__dict__"):
        return {k: v for k, v in vars(o).items() if not k.startswith("_")}
    return str(o)


def content_hash(*parts: Any) -> str:
    """blake2b over canonical content. Deterministic across processes/machines."""
    h = hashlib.blake2b(digest_size=_HASH_LEN)
    for p in parts:
        h.update(canonical(p).encode("utf-8"))
        h.update(b"\x1f")  # unit separator: prevents ("ab","c") == ("a","bc")
    return h.hexdigest()


def chain_hash(prev: str, payload: Any) -> str:
    """Hash-chain link: binds a record to its predecessor.

    Gives the ledger tamper-evidence -- altering any historical record breaks
    every hash after it, which the integrity verifier detects immediately.
    """
    return content_hash(prev or "GENESIS", payload)


def signal_id(session_date: str, seq: int, seat: str, direction: str) -> str:
    """Sortable, self-describing, guessable-from-path.

    SIG-20260801-0007-B-short  ->  you can find this file without an index.
    Naming survives index loss; the index only makes it fast.
    """
    return f"SIG-{session_date.replace('-', '')}-{seq:04d}-{seat}-{direction}"


def frame_id(t_event_ns: int, schema_version: str) -> str:
    return f"FRM-{t_event_ns}-{schema_version}"


def incident_id(session_date: str, seq: int, kind: str) -> str:
    return f"INC-{session_date.replace('-', '')}-{seq:04d}-{kind}"


def lesson_slug(text: str) -> str:
    """Stable slug for a lesson tag -> becomes a directory name in the library."""
    keep = [c.lower() if c.isalnum() else "_" for c in text.strip()]
    slug = "".join(keep)
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug.strip("_")[:64]
