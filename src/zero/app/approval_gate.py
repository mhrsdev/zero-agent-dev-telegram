"""Per-call tool approval gate (GAP 8b/G2, Hermes parity).

Zero historically authorized work at the PLAN level only: approve a
revision and every tool call inside its execution ran unchecked. Hermes
adds a layered per-call gate on top of mission approval; this service
ports the portable core:

1. **Hardline floor** — catastrophic argument shapes are refused even
   when every allowlist says yes, and even in ``manual`` mode.
2. **Deny rules outrank allows** — a matching deny row short-circuits
   any standing allow (wildcard ``args_hash=''`` rows apply to all
   argument shapes of the tool).
3. **Standing always-allows** — durable, restart-surviving grants keyed
   by project + tool + canonical argument hash.
4. **Pending queue with TTL** — an un-decided request expires so the
   runtime never blocks forever: it tells the model "approval pending"
   as a structured tool error and moves on.

The gate is opt-in per deployment via ``ZERO_TOOL_APPROVAL_MODE``:
``off`` (default) keeps historical behavior byte-for-byte; ``manual``
consults this service before executing each declared tool call;
``auto`` (2026-08-31, live-run B12a) enforces ONLY the hardline floor
and deny rules — no human loop — so autonomous pipelines can run
unattended while catastrophic calls and operator deny rules still fail
closed. ``manual`` additionally auto-allows PROVABLY read-only calls
(B12b, Hermes triage parity: ``read_file`` / ``capture_diff`` /
read-only ``run_command`` shapes) — the live run showed an agent
paralyzed because even ``ls`` and ``git status`` needed a human click.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from typing import Any

from zero.domain.approvals import ApprovalVerdict, ToolApprovalRequest
from zero.domain.ids import generate_tool_approval_id


def _compile(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


#: Hardline floor: patterns that are unrecoverable-catastrophic regardless
#: of any allow decision. Mirrors tools/approval.py HARDLINE_PATTERNS.
_HARDLINE_PATTERNS: tuple[re.Pattern[str], ...] = _compile(
    (
        r"rm\s+(-[a-z]*r[a-z]*f|--recursive\s+--force)\s+/(\s|$)",
        r"mkfs(\.\w+)?\s",
        r"dd\s+[^\n]*of=/dev/(?:sd|nvme|hd|vd)",
        r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",  # fork bomb
        r"(shutdown|reboot|halt|poweroff)(\s|$)",
        r">\s*/dev/sd[a-z]",
    )
)


class ApprovalError(RuntimeError):
    """Typed failure raised by resolve/list operations."""


#: B12b (2026-08-31): git subcommands whose ENTIRE surface is read-only;
#: safe for manual-mode auto-allow. Mutable subcommands (add/commit/
#: checkout/reset/push/...) are deliberately absent.
_READONLY_GIT_SUBCOMMANDS = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "branch",
        "rev-parse",
        "ls-files",
        "cat-file",
        "describe",
        "blame",
        "shortlog",
        "grep",
    }
)


def _is_safe_readonly(tool_name: str, input_data: dict[str, Any]) -> bool:
    """Classify a tool call as provably read-only (manual-mode triage).

    B12b (live run 2026-08-31): manual mode gated EVERY call, including
    ``ls``/``git status``/``read_file``; the live agent burned its whole
    attempt on pending approvals for pure reads and finalized with
    "I could not perform the task". Hermes triage: reads flow, writes
    ask.
    """
    if tool_name in ("read_file", "capture_diff"):
        return True
    if tool_name != "run_command":
        return False
    command = input_data.get("command")
    if not isinstance(command, str) or not command:
        return False
    args = input_data.get("args") or []
    if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
        return False
    if command in ("ls", "grep"):
        return True
    if command == "git":
        if not args:
            return False
        sub = args[0].strip().lower()
        if sub in _READONLY_GIT_SUBCOMMANDS:
            return True
        if sub == "worktree":
            rest = [a.strip().lower() for a in args[1:]]
            return bool(rest) and rest[0] == "list"
        return False
    return False


class ToolNotFoundDuringApproval(ApprovalError):
    pass


class ToolApprovalGate:
    """Consultable pre-execution gate over durable decisions."""

    def __init__(
        self,
        database: Any,
        *,
        mode: str = "manual",
        pending_ttl_seconds: float = 600.0,
        clock: Any = time.time,
    ) -> None:
        if mode not in ("off", "manual", "auto"):
            raise ValueError("approval mode must be 'off', 'manual' or 'auto'")
        self._db = database
        self._mode = mode
        self._ttl = pending_ttl_seconds
        self._clock = clock
        # session grain lives in-process only, keyed by
        # (project_id, execution_id or '', tool_name)
        self._session_grants: set[tuple[str, str, str]] = set()
        self._lock = threading.Lock()
        # Hermes-parity notify hook (2026-08-31): fired when a NEW pending
        # request is minted so the messaging surface can push the approval
        # card (inline buttons) instead of waiting for the model to give up
        # or the operator to poll /approvals.
        self._notify: Any = None

    @property
    def mode(self) -> str:
        return self._mode

    def attach_notifier(self, callback: Any) -> None:
        """Register the pending-request notifier (best-effort by design)."""
        self._notify = callback

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def canonical_args_hash(input_data: dict[str, Any]) -> str:
        canonical = json.dumps(input_data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def hardline_match(tool_name: str, input_data: dict[str, Any]) -> str | None:
        blob = f"{tool_name}\n{json.dumps(input_data, ensure_ascii=False)}"
        # B12a hardening (2026-08-31): argv-shaped JSON separates tokens
        # with quotes/commas AND interleaves key names ("command": "rm",
        # "args": ["-rf", "/"]) so the catastrophic patterns — written
        # for shell-string shapes — never saw them. Strip JSON keys,
        # then separators, and match both shapes; fail-closed bias is
        # intentional for the hardline floor.
        normalized = re.sub(r'"[^"\\]*"\s*:', " ", blob)
        normalized = re.sub(r'["\[\]{},\\]', " ", normalized)
        for pattern in _HARDLINE_PATTERNS:
            match = pattern.search(blob) or pattern.search(normalized)
            if match:
                return pattern.pattern
        return None

    @staticmethod
    def _row_to_request(row: Any) -> ToolApprovalRequest:
        keys = (
            "id",
            "project_id",
            "execution_id",
            "tool_name",
            "args_hash",
            "grain",
            "decision",
            "decided_by_user_id",
            "reason",
            "created_at",
            "resolved_at",
        )
        data = dict(row)
        return ToolApprovalRequest(**{k: data[k] for k in keys})

    def _fetch_one(self, sql: str, params: tuple = ()) -> ToolApprovalRequest | None:
        conn = self._db.connect()
        row = conn.execute(sql, params).fetchone()
        return self._row_to_request(row) if row is not None else None

    # ------------------------------------------------------------------
    # evaluation path (called by AgentRuntime per tool call)
    # ------------------------------------------------------------------

    def evaluate(
        self,
        *,
        project_id: str,
        execution_id: str | None,
        tool_name: str,
        input_data: dict[str, Any],
    ) -> ApprovalVerdict:
        """Decide what to do with one concrete tool call right now."""
        if self._mode == "off":
            return ApprovalVerdict(state="allowed", cause="mode_off")

        hardline = self.hardline_match(tool_name, input_data)
        if hardline is not None:
            return ApprovalVerdict(state="denied", cause=f"hardline:{hardline}")

        args_hash = self.canonical_args_hash(input_data)

        with self._lock:
            if (project_id, execution_id or "", tool_name) in self._session_grants:
                return ApprovalVerdict(state="allowed", cause="session_grant", request=None)

        # Deny rules outrank everything except hardline. Wildcard rows
        # (args_hash='') block every shape of the tool.
        try:
            rule_row = (
                self._db.connect()
                .execute(
                    "SELECT * FROM tool_approval_decisions "
                    "WHERE project_id = ? AND tool_name = ? AND decision = 'deny' "
                    "AND grain IN ('always', 'once') AND args_hash IN (?, '') "
                    "ORDER BY created_at DESC LIMIT 1",
                    (project_id, tool_name, args_hash),
                )
                .fetchone()
            )
        except sqlite3.Error as exc:
            raise ApprovalError(f"deny-rule lookup failed: {exc}") from exc
        if rule_row is not None:
            request = self._row_to_request(rule_row)
            return ApprovalVerdict(state="denied", cause="rule", request=request)

        # B12a (2026-08-31): auto mode = unattended operation with the
        # SAME safety floor. Hardline and operator deny rules were already
        # enforced above; everything else flows without a human click.
        if self._mode == "auto":
            return ApprovalVerdict(state="allowed", cause="mode_auto")

        # B12b: manual-mode triage — provably read-only calls never need
        # a human click (deny rules above still outrank this allow).
        if _is_safe_readonly(tool_name, input_data):
            return ApprovalVerdict(state="allowed", cause="safe_readonly")

        try:
            standing_row = (
                self._db.connect()
                .execute(
                    "SELECT * FROM tool_approval_decisions "
                    "WHERE project_id = ? AND tool_name = ? AND decision = 'allow' "
                    "AND grain = 'always' AND args_hash IN (?, '') "
                    "ORDER BY (CASE WHEN args_hash = '' THEN 1 ELSE 0 END), created_at DESC "
                    "LIMIT 1",
                    (project_id, tool_name, args_hash),
                )
                .fetchone()
            )
        except sqlite3.Error as exc:
            raise ApprovalError(f"standing-allow lookup failed: {exc}") from exc
        if standing_row is not None:
            return ApprovalVerdict(
                state="allowed",
                cause="standing_allow",
                request=self._row_to_request(standing_row),
            )

        # Reusable existing pending window (dedupes N identical calls).
        try:
            pending_row = (
                self._db.connect()
                .execute(
                    "SELECT * FROM tool_approval_decisions "
                    "WHERE project_id = ? AND tool_name = ? AND args_hash = ? "
                    "AND decision IS NULL AND execution_id IS ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (project_id, tool_name, args_hash, execution_id),
                )
                .fetchone()
            )
        except sqlite3.Error as exc:
            raise ApprovalError(f"pending lookup failed: {exc}") from exc
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        if pending_row is not None:
            request = self._row_to_request(pending_row)
            if self._request_fresh(request):
                return ApprovalVerdict(state="pending", request=request)
            # expired: close it out so a fresh pending window re-issues
            self._resolve_stored(request.id, decision=None, actor=None, grain="once")

        request_id = generate_tool_approval_id()
        try:
            conn = self._db.connect()
            conn.execute(
                "INSERT INTO tool_approval_decisions "
                "(id, project_id, execution_id, tool_name, args_hash, grain, decision, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'once', NULL, ?)",
                (request_id, project_id, execution_id, tool_name, args_hash, now_iso),
            )
            self._db.commit()
        except sqlite3.Error as exc:
            raise ApprovalError(f"pending insert failed: {exc}") from exc
        request = self.get(request_id)
        if self._notify is not None and request is not None:
            # Notification is presentation, never authority: a failing
            # sink must not change the verdict the runtime acts on.
            try:
                self._notify(request)
            except Exception:  # noqa: BLE001 - notify must never break the gate
                import logging

                logging.getLogger(__name__).debug(
                    "tool approval notifier failed", exc_info=True
                )
        return ApprovalVerdict(state="pending", request=request)

    def _request_fresh(self, request: ToolApprovalRequest) -> bool:
        try:
            created = time.mktime(time.strptime(request.created_at, "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            return False
        return (self._clock() - created) < self._ttl

    # ------------------------------------------------------------------
    # resolution + listing (REST surface)
    # ------------------------------------------------------------------

    def get(self, request_id: str) -> ToolApprovalRequest | None:
        return self._fetch_one("SELECT * FROM tool_approval_decisions WHERE id = ?", (request_id,))

    def list_pending(self, *, project_id: str) -> list[ToolApprovalRequest]:
        rows = (
            self._db.connect()
            .execute(
                "SELECT * FROM tool_approval_decisions WHERE project_id = ? AND decision IS NULL "
                "ORDER BY created_at DESC LIMIT 200",
                (project_id,),
            )
            .fetchall()
        )
        return [self._row_to_request(r) for r in rows]

    def resolve(
        self,
        request_id: str,
        *,
        decision: str,
        decided_by_user_id: str | None,
        grain: str = "once",
        reason: str | None = None,
    ) -> ToolApprovalRequest:
        """Record a human verdict on a pending request.

        Grain semantics: ``once`` scopes the effect to nothing beyond
        this record; ``session`` grants in-process reuse inside the same
        execution; ``always`` makes it durable - allows keyed to the
        canonical argument hash (falling back to a tool-wide row), and
        denials escalate to a TOOL-WIDE wildcard rule (Hermes parity:
        deny lists are coarse by design).
        """
        if decision not in ("allow", "deny"):
            raise ApprovalError("decision must be 'allow' or 'deny'")
        if grain not in ("once", "session", "always"):
            raise ApprovalError("grain must be 'once', 'session', or 'always'")
        request = self.get(request_id)
        if request is None:
            raise ToolNotFoundDuringApproval(f"tool approval {request_id} not found")
        if request.decision is not None:
            return request  # idempotent double-resolve returns current state
        resolved_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        self._resolve_stored(
            request_id,
            decision=decision,
            actor=decided_by_user_id,
            grain=grain if decision == "allow" else "once",
            reason=reason,
            resolved_at=resolved_iso,
        )
        updated = self.get(request_id)
        assert updated is not None
        if decision == "allow" and grain == "session":
            with self._lock:
                self._session_grants.add(
                    (
                        updated.project_id,
                        updated.execution_id or "",
                        updated.tool_name,
                    )
                )
        if decision == "deny" and grain == "always":
            # A permanent denial blocks EVERY argument shape of the tool:
            # write the wildcard companion row so future evaluations match
            # args_hash IN (?, ''). The exact-shape row stays as audit.
            try:
                conn = self._db.connect()
                conn.execute(
                    "INSERT OR IGNORE INTO tool_approval_decisions "
                    "(id, project_id, execution_id, tool_name, args_hash, grain, "
                    "decision, decided_by_user_id, reason, created_at, resolved_at) "
                    "VALUES (?, ?, NULL, ?, '', 'always', 'deny', ?, ?, ?, ?)",
                    (
                        generate_tool_approval_id(),
                        updated.project_id,
                        updated.tool_name,
                        updated.decided_by_user_id,
                        updated.reason,
                        updated.created_at,
                        updated.resolved_at,
                    ),
                )
                self._db.commit()
            except sqlite3.Error as exc:
                raise ApprovalError(f"wildcard deny write failed: {exc}") from exc
        return updated

    def _resolve_stored(
        self,
        request_id: str,
        *,
        decision: str | None,
        actor: str | None,
        grain: str,
        reason: str | None = None,
        resolved_at: str | None = None,
    ) -> None:
        try:
            conn = self._db.connect()
            conn.execute(
                "UPDATE tool_approval_decisions SET decision = ?, decided_by_user_id = ?, "
                "grain = ?, reason = COALESCE(?, reason), resolved_at = ? WHERE id = ?",
                (decision, actor, grain, reason, resolved_at, request_id),
            )
            self._db.commit()
        except sqlite3.Error as exc:
            raise ApprovalError(f"resolve failed: {exc}") from exc
