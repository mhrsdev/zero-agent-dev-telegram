"""Zero v2 path security — ported from Hermes (``tools/path_security.py``).

Path traversal protection for file operations.
"""
from __future__ import annotations

from pathlib import Path

__all__ = ["PathTraversalError", "has_traversal_component", "validate_within_dir"]


class PathTraversalError(RuntimeError):
    """Raised when a path escapes the allowed root."""


def has_traversal_component(path_str: str) -> bool:
    """Quick check: does ``path_str`` contain a ``..`` component?"""
    if not path_str:
        return False
    parts = Path(path_str).parts
    return ".." in parts


def validate_within_dir(path: str | Path, root: str | Path) -> Path:
    """Validate that ``path`` resolves to a location inside ``root``.

    Follows symlinks, normalizes ``..``, then verifies containment via
    ``relative_to(root)``.

    Raises :class:`PathTraversalError` if ``path`` escapes ``root``.
    Returns the resolved :class:`pathlib.Path` on success.
    """
    p = Path(path).resolve()
    r = Path(root).resolve()
    try:
        p.relative_to(r)
    except ValueError as e:
        raise PathTraversalError(
            f"path {path!r} resolves to {p} which is outside allowed root {r}"
        ) from e
    return p
