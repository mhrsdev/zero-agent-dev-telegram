"""Zero management layer (installer / wizard / TUI / GUI / CLI).

Milestone M0 ground: package root + `zero` console entry. Subcommands land
milestone-by-milestone per docs/management-layer-plan/13-implementation-plan.md
and are intentionally absent until implemented — no placeholder UX.
"""

from __future__ import annotations

import argparse

from zero import __version__

__all__ = ["main"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zero",
        description="Zero Dev Telegram — install, configure, operate",
    )
    parser.add_argument("--version", action="version", version=f"zero {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    # M0: only version/global flags exist; argparse exits on --version.
    del args
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
