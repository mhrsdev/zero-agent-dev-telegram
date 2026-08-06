"""Zero v2 security package — Phase 8.

Approval workflow, SSRF ``net_guard``, persistent revocable session,
path traversal protection, threat patterns database.
"""
from __future__ import annotations

from zero.security.approval import (
    ApprovalChoice,
    ApprovalRequest,
    ApprovalResolver,
    ApprovalStatus,
    ApprovalStore,
)
from zero.security.net_guard import NetGuard, NetGuardError, check_url
from zero.security.path import validate_within_dir
from zero.security.session import Session, SessionStore
from zero.security.threat_patterns import scan_content, scan_strict

__all__ = [
    "ApprovalChoice",
    "ApprovalRequest",
    "ApprovalResolver",
    "ApprovalStatus",
    "ApprovalStore",
    "NetGuard",
    "NetGuardError",
    "Session",
    "SessionStore",
    "check_url",
    "scan_content",
    "scan_strict",
    "validate_within_dir",
]
