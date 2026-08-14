"""``python -m celpix_lint`` — the same entry point as the console script."""

from __future__ import annotations

from celpix_lint.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
