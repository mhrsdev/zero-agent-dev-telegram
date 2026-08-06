"""V4A patch parser — ported from Hermes (``tools/patch_parser.py``).

Format:
    *** Begin Patch
    *** Update File: path/to/file.py
    @@ optional context hint @@
     context line (space prefix)
    -removed line (minus prefix)
    +added line (plus prefix)
    *** Add File: path/to/new.py
    +new file content
    *** Delete File: path/to/old.py
    *** Move File: old/path.py -> new/path.py
    *** End Patch
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

__all__ = [
    "OperationType",
    "HunkLine",
    "Hunk",
    "PatchOperation",
    "parse_patch",
    "apply_patch",
]


class OperationType(str, Enum):
    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"
    MOVE = "move"


@dataclass(frozen=True, slots=True)
class HunkLine:
    """A single line in a hunk: context, removed, or added."""

    prefix: str  # " " (context), "-" (removed), "+" (added)
    content: str


@dataclass(slots=True)
class Hunk:
    """A hunk within an update operation."""

    context_hint: str | None
    lines: list[HunkLine] = field(default_factory=list)


@dataclass(slots=True)
class PatchOperation:
    """A single patch operation (add/update/delete/move)."""

    op_type: OperationType
    path: str
    new_path: str | None = None  # for MOVE
    hunks: list[Hunk] = field(default_factory=list)
    content: str | None = None  # for ADD (full file content)


_FILE_HEADER_RE = re.compile(
    r"^\*\*\* (Begin Patch|End Patch|Update File:|Add File:|Delete File:|Move File:)"
)
_HUNK_HEADER_RE = re.compile(r"^@@(.*)@@$")


def parse_patch(patch_text: str) -> list[PatchOperation]:
    """Parse V4A patch text into a list of operations.

    Raises ValueError on malformed input.
    """
    lines = patch_text.splitlines()
    operations: list[PatchOperation] = []
    i = 0

    # Skip until *** Begin Patch
    while i < len(lines) and lines[i].strip() != "*** Begin Patch":
        i += 1
    if i >= len(lines):
        raise ValueError("patch does not contain '*** Begin Patch'")
    i += 1  # skip Begin Patch line

    current_op: PatchOperation | None = None
    current_hunk: Hunk | None = None

    while i < len(lines):
        line = lines[i]

        if line.strip() == "*** End Patch":
            break

        # File headers.
        if line.startswith("*** Update File: "):
            if current_op is not None:
                if current_hunk is not None:
                    current_op.hunks.append(current_hunk)
                    current_hunk = None
                operations.append(current_op)
            path = line[len("*** Update File: "):].strip()
            current_op = PatchOperation(op_type=OperationType.UPDATE, path=path)
            current_hunk = None
        elif line.startswith("*** Add File: "):
            if current_op is not None:
                if current_hunk is not None:
                    current_op.hunks.append(current_hunk)
                    current_hunk = None
                operations.append(current_op)
            path = line[len("*** Add File: "):].strip()
            current_op = PatchOperation(op_type=OperationType.ADD, path=path)
        elif line.startswith("*** Delete File: "):
            if current_op is not None:
                if current_hunk is not None:
                    current_op.hunks.append(current_hunk)
                    current_hunk = None
                operations.append(current_op)
            path = line[len("*** Delete File: "):].strip()
            current_op = PatchOperation(op_type=OperationType.DELETE, path=path)
            operations.append(current_op)
            current_op = None
        elif line.startswith("*** Move File: "):
            if current_op is not None:
                if current_hunk is not None:
                    current_op.hunks.append(current_hunk)
                    current_hunk = None
                operations.append(current_op)
            rest = line[len("*** Move File: "):].strip()
            # Format: old/path.py -> new/path.py
            if " -> " not in rest:
                raise ValueError(f"invalid Move File line: {line!r}")
            old_path, new_path = rest.split(" -> ", 1)
            current_op = PatchOperation(
                op_type=OperationType.MOVE,
                path=old_path.strip(),
                new_path=new_path.strip(),
            )
            operations.append(current_op)
            current_op = None
        elif line.startswith("@@"):
            # Hunk header: @@ context hint @@ OR @@ context hint
            if current_op is None:
                raise ValueError(f"hunk header outside of operation: {line!r}")
            if current_hunk is not None:
                current_op.hunks.append(current_hunk)
            # Strip @@ prefix and optional @@ suffix.
            rest = line[2:]
            if rest.endswith("@@"):
                rest = rest[:-2]
            context_hint = rest.strip() or None
            current_hunk = Hunk(context_hint=context_hint)
        elif current_op is not None and current_op.op_type is OperationType.ADD:
            # Add file content (lines starting with +).
            if line.startswith("+"):
                if current_op.content is None:
                    current_op.content = ""
                current_op.content += line[1:] + "\n"
            # Ignore other lines in ADD.
        elif current_hunk is not None:
            # Hunk line.
            if line.startswith(" "):
                current_hunk.lines.append(HunkLine(prefix=" ", content=line[1:]))
            elif line.startswith("-"):
                current_hunk.lines.append(HunkLine(prefix="-", content=line[1:]))
            elif line.startswith("+"):
                current_hunk.lines.append(HunkLine(prefix="+", content=line[1:]))
            elif line == "":
                # Empty line in hunk = context line.
                current_hunk.lines.append(HunkLine(prefix=" ", content=""))
            else:
                raise ValueError(f"invalid hunk line: {line!r}")

        i += 1

    # Don't forget the last operation.
    if current_op is not None:
        if current_hunk is not None:
            current_op.hunks.append(current_hunk)
        operations.append(current_op)

    return operations


def apply_patch(operations: list[PatchOperation]) -> list[str]:
    """Apply a list of patch operations. Returns list of result messages.

    Raises FileNotFoundError if an UPDATE/DELETE/MOVE source doesn't exist.
    """
    results: list[str] = []
    for op in operations:
        if op.op_type is OperationType.ADD:
            path = Path(op.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(op.content or "", encoding="utf-8")
            results.append(f"added: {op.path}")
        elif op.op_type is OperationType.UPDATE:
            path = Path(op.path)
            if not path.exists():
                raise FileNotFoundError(f"cannot update non-existent file: {op.path}")
            original_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            new_lines = _apply_hunks(original_lines, op.hunks)
            path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            results.append(f"updated: {op.path}")
        elif op.op_type is OperationType.DELETE:
            path = Path(op.path)
            if not path.exists():
                raise FileNotFoundError(f"cannot delete non-existent file: {op.path}")
            path.unlink()
            results.append(f"deleted: {op.path}")
        elif op.op_type is OperationType.MOVE:
            old_path = Path(op.path)
            new_path = Path(op.new_path or "")
            if not old_path.exists():
                raise FileNotFoundError(f"cannot move non-existent file: {op.path}")
            new_path.parent.mkdir(parents=True, exist_ok=True)
            old_path.rename(new_path)
            results.append(f"moved: {op.path} -> {op.new_path}")
    return results


def _apply_hunks(original_lines: list[str], hunks: list[Hunk]) -> list[str]:
    """Apply hunks to original lines. Returns new lines.

    Algorithm: for each hunk, find the anchor (sequence of context+removed
    lines) in the original, then replace that block with context+added lines.
    """
    result = list(original_lines)
    for hunk in hunks:
        # Build the anchor (what to search for in original).
        anchor_lines: list[str] = []
        # Build the replacement (what to put in its place).
        replacement_lines: list[str] = []
        for hl in hunk.lines:
            if hl.prefix == " ":
                anchor_lines.append(hl.content)
                replacement_lines.append(hl.content)
            elif hl.prefix == "-":
                anchor_lines.append(hl.content)
            elif hl.prefix == "+":
                replacement_lines.append(hl.content)

        if not anchor_lines:
            # All-added hunk — insert at end.
            result.extend(replacement_lines)
            continue

        # Find anchor in result.
        anchor_pos = _find_anchor(result, anchor_lines)
        if anchor_pos is None:
            # Try fuzzy match (first line only).
            for i, line in enumerate(result):
                if line.strip() == anchor_lines[0].strip():
                    anchor_pos = i
                    break
        if anchor_pos is None:
            raise ValueError(
                f"cannot find anchor for hunk: {hunk.context_hint or ''} "
                f"(looking for: {anchor_lines[:3]!r})"
            )

        # Replace the anchor block with replacement.
        result[anchor_pos:anchor_pos + len(anchor_lines)] = replacement_lines

    return result


def _find_anchor(lines: list[str], anchor_lines: list[str]) -> int | None:
    """Find the position where anchor_lines appears in lines."""
    if not anchor_lines:
        return None
    for i in range(len(lines) - len(anchor_lines) + 1):
        if lines[i:i + len(anchor_lines)] == anchor_lines:
            return i
    return None
