"""Filesystem-backed markdown memory store.

Each unit of memory is a `.md` file with optional YAML-ish frontmatter. The
brain reads files via this module; the operator can edit the same files
directly with a text editor.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_DIGEST = """# What ATLAS Keeps Getting Wrong

(Memory empty. No autopsies yet. The system has not earned an opinion.)
"""


@dataclass
class AutopsyRecord:
    autopsy_id: str
    instrument: str
    setup: Optional[str]
    regime: Optional[str]
    result: str  # "won" | "lost" | "skipped" | "breakeven"
    r_multiple: float
    kill_thesis_triggered: bool
    tags: List[str] = field(default_factory=list)
    body: str = ""


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Parse a leading `---\\n...\\n---\\n` block as JSON-shaped key/value.

    The writer emits `<key>: <json-value>` per line. Returns (frontmatter, body)
    where frontmatter is empty if no block is present.
    """
    if not text.startswith("---\n"):
        return {}, text
    closing = text.find("\n---\n", 4)
    if closing == -1:
        return {}, text
    block = text[4:closing]
    body = text[closing + 5 :]
    fm: Dict[str, Any] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        v = v.strip()
        try:
            fm[k.strip()] = json.loads(v) if v else None
        except json.JSONDecodeError:
            fm[k.strip()] = v
    return fm, body


class MarkdownMemory:
    """Read/write the `memory/` directory tree."""

    def __init__(self, root: str | Path = "memory/") -> None:
        self.root = Path(root)
        self.autopsies_dir = self.root / "autopsies"
        self.setups_dir = self.root / "setups"
        self.regimes_dir = self.root / "regimes"
        self.journal_dir = self.root / "journal"
        self.digest_path = self.root / "digest.md"
        self.index_path = self.root / "_index.md"
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        for d in (self.autopsies_dir, self.setups_dir, self.regimes_dir, self.journal_dir):
            d.mkdir(parents=True, exist_ok=True)
        if not self.digest_path.exists():
            self.digest_path.write_text(DEFAULT_DIGEST)

    # -- digest -----------------------------------------------------------
    def read_digest(self) -> str:
        return self.digest_path.read_text() if self.digest_path.exists() else DEFAULT_DIGEST

    def write_digest(self, content: str) -> None:
        self.digest_path.write_text(content)

    # -- autopsies --------------------------------------------------------
    def write_autopsy(self, record: AutopsyRecord) -> Path:
        path = self.autopsies_dir / f"{record.autopsy_id}.md"
        frontmatter = {
            "id": record.autopsy_id,
            "instrument": record.instrument,
            "setup": record.setup,
            "regime": record.regime,
            "result": record.result,
            "r_multiple": record.r_multiple,
            "kill_thesis_triggered": record.kill_thesis_triggered,
            "tags": record.tags,
            "written_at": int(time.time()),
        }
        fm_lines = "\n".join(
            f"{k}: {json.dumps(v, default=str)}" for k, v in frontmatter.items()
        )
        path.write_text(f"---\n{fm_lines}\n---\n\n{record.body.strip()}\n")
        return path

    def list_autopsies(self) -> List[Path]:
        return sorted(self.autopsies_dir.glob("*.md"))

    def load_autopsy(self, path: Path) -> Tuple[Dict[str, Any], str]:
        return parse_frontmatter(path.read_text())

    def recent_autopsies(self, limit: int = 10) -> List[Tuple[Dict[str, Any], str]]:
        paths = self.list_autopsies()[-limit:]
        return [self.load_autopsy(p) for p in paths]

    # -- setups -----------------------------------------------------------
    def list_setups(self) -> List[Path]:
        return sorted(self.setups_dir.glob("*.md"))

    def read_setup(self, name: str) -> Optional[str]:
        path = self.setups_dir / f"{name}.md"
        return path.read_text() if path.exists() else None

    def write_setup(self, name: str, content: str) -> Path:
        path = self.setups_dir / f"{name}.md"
        path.write_text(content)
        return path

    def relevant_setups(
        self,
        tags: Optional[List[str]] = None,
        max_results: int = 3,
        max_chars: int = 1500,
    ) -> List[str]:
        """Return summaries of setup files matching any of the given tags.

        Tags match against any substring of the setup file (tag list, prose, or
        title). When tags are None or empty, returns the first N setup files.
        """
        out: List[str] = []
        needles = [t.lower() for t in tags] if tags else None
        for path in self.list_setups():
            text = path.read_text()
            if needles is not None:
                if not any(n in text.lower() for n in needles):
                    continue
            out.append(f"### {path.stem}\n{text[:max_chars]}")
            if len(out) >= max_results:
                break
        return out

    # -- index ------------------------------------------------------------
    def index_summary(self) -> str:
        if self.index_path.exists():
            return self.index_path.read_text()
        return self._build_default_index()

    def _build_default_index(self) -> str:
        return (
            "# Memory Index\n\n"
            f"- Autopsies: {len(self.list_autopsies())}\n"
            f"- Setups: {len(self.list_setups())}\n"
        )
