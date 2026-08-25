"""Access-policy core: pure decision function + engine gate factory.

The gate is injected into InterfaceAdapterService as ``policy_gate`` so the
engine never imports the management layer (dependency inversion).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

AccessMode = Literal["owner_only", "users", "groups", "users_and_groups", "public"]

FEATURES = {"chat", "plan", "approve", "search"}


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str  # stable code, safe to expose


ALLOW = PolicyDecision(True, "ok")


def decide(
    *,
    mode: str,
    sender_external_id: str,
    chat_id: str,
    owner_external_id: str | None,
    allow_users: list[str],
    groups: list[dict[str, Any]],
    feature: str = "chat",
) -> PolicyDecision:
    """Pure access decision. Denials never leak configuration."""
    if mode == "public":
        return ALLOW

    is_owner = owner_external_id is not None and sender_external_id == owner_external_id
    if mode == "owner_only":
        return ALLOW if is_owner else PolicyDecision(False, "policy_owner_only")

    group = next((g for g in groups if str(g.get("chat_id")) == str(chat_id)), None)
    in_allowed_group = group is not None and bool(group.get("enabled", True))
    in_allow_users = sender_external_id in set(allow_users)

    if mode == "users":
        ok = in_allow_users or is_owner
        return ALLOW if ok else PolicyDecision(False, "policy_user_not_listed")
    if mode == "groups":
        ok = in_allowed_group or is_owner
        return ALLOW if ok else PolicyDecision(False, "policy_group_not_allowed")
    if mode == "users_and_groups":
        ok = in_allow_users or in_allowed_group or is_owner
        return ALLOW if ok else PolicyDecision(False, "policy_not_listed")
    # Unknown mode fails closed.
    return PolicyDecision(False, "policy_unknown_mode")


def feature_gate(group: dict[str, Any] | None, feature: str) -> PolicyDecision:
    if group is None:
        return ALLOW
    features = set(group.get("allowed_features") or ["chat"])
    if feature in features:
        return ALLOW
    return PolicyDecision(False, f"feature_{feature}_disabled_for_group")


def rate_limit_ok(bucket: dict[str, list[float]], key: str, per_min: int) -> bool:
    """Sliding-minute window; bucket mutated in place by caller contract."""
    now = time.monotonic()
    hits = [t for t in bucket.get(key, []) if now - t < 60.0]
    if len(hits) >= per_min:
        bucket[key] = hits
        return False
    hits.append(now)
    bucket[key] = hits
    return True


def _group_as_dict(group: Any) -> dict[str, Any]:
    """Normalize a group entry to a plain dict.

    Accepts dicts (managed config path) and objects exposing either
    ``model_dump()`` or matching attributes (GroupPolicy dataclass path).
    """
    if isinstance(group, dict):
        return group
    dump = getattr(group, "model_dump", None)
    if callable(dump):
        dumped = dump()
        if isinstance(dumped, dict):
            return dumped
    return {
        key: getattr(group, key)
        for key in ("chat_id", "title", "enabled", "allowed_features")
        if hasattr(group, key)
    }


def build_gate(
    cfg_getter: Callable[[], Any],
    owner_lookup: Callable[[str], str | None],
) -> Callable[[str, str, str, str], PolicyDecision]:
    """Return gate(platform, external_actor_id, chat_id, feature).

    ``cfg_getter`` returns the live AccessCfg-like object each call so
    policy edits apply without restart; ``owner_lookup`` maps project id →
    owner's linked telegram external id (or None).
    """

    def gate(
        platform: str,
        sender_id: str,
        chat_id: str,
        feature: str = "chat",
    ) -> PolicyDecision:
        cfg = cfg_getter()
        if cfg is None:
            return ALLOW
        owner_ext = None
        try:
            owner_ext = owner_lookup(cfg.owner_project_id)
        except (OSError, RuntimeError, LookupError):
            owner_ext = None
        groups = [_group_as_dict(g) for g in getattr(cfg, "groups", [])]
        base = decide(
            mode=cfg.mode,
            sender_external_id=sender_id,
            chat_id=chat_id,
            owner_external_id=owner_ext,
            allow_users=list(cfg.allow_users),
            groups=groups,
            feature=feature,
        )
        if not base.allowed:
            return base
        group = next(
            (g for g in groups if str(g.get("chat_id")) == str(chat_id)),
            None,
        )
        return feature_gate(group, feature)

    return gate
