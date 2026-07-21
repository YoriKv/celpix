"""Trust gate for code plugins.

A ``*.py`` plugin runs with the app's privileges, so before Celpix executes one it
asks the user to approve it, and remembers the approval so it loads silently next
time. Trust is keyed on the **content hash**, not the path: approving a plugin
trusts *that exact code*. If the file's bytes change, the hash changes and the user
is asked again — moving/renaming keeps trust, editing the code does not.

This module is Qt-free. The confirmation itself is a callback the caller injects
(the app supplies a Qt dialog; tests supply a stub), so the trust policy stays
testable and headless. TOML presets are pure data and never gated — only code is.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


def digest_bytes(data: bytes) -> str:
    """The SHA-256 hex digest that identifies a plugin's exact code."""
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class PendingCodePlugin:
    """A code plugin awaiting the user's decision. Passed to the confirm callback."""

    path: str
    digest: str


# Returns True to load the plugin, False to skip it. Injected by the caller.
ConfirmCallback = Callable[[PendingCodePlugin], bool]


class TrustStore:
    """Remembers which code-plugin hashes the user has approved.

    Backed by a JSON file when ``path`` is given; purely in-memory otherwise (used
    by tests). A corrupt or unreadable store starts empty rather than crashing —
    the worst case is re-prompting, never silently trusting.
    """

    def __init__(self, path: str | None = None) -> None:
        self._path = path
        self._trusted: dict[str, str] = {}  # digest -> last-seen label (path)
        # Paths approved during *this* run. Lets a developer edit-and-refresh a
        # plugin they already okayed without a prompt on every change, while a
        # brand-new launch still re-prompts for changed code (this set starts
        # empty each run). Session-only — never persisted.
        self._session_paths: set[str] = set()
        if path is not None:
            self._load()

    def is_trusted(self, digest: str) -> bool:
        return digest in self._trusted

    def is_session_path(self, label: str) -> bool:
        """True if this path was approved earlier in this run (developer loop)."""
        return label in self._session_paths

    def trust(self, digest: str, label: str) -> None:
        self._trusted[digest] = label
        self._session_paths.add(label)
        self._save()

    def _load(self) -> None:
        try:
            data = json.loads(Path(self._path).read_text(encoding="utf-8"))
            trusted = data.get("trusted", {})
            if isinstance(trusted, dict):
                self._trusted = {str(k): str(v) for k, v in trusted.items()}
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 — corrupt store: start empty, never crash
            pass

    def _save(self) -> None:
        if self._path is None:
            return
        target = Path(self._path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"trusted": self._trusted}, indent=2), encoding="utf-8"
        )
