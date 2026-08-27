"""Stable ID generation utilities.

IDs are server-issued, never derived from a name or external
identifier. They use a short prefix to make logs and URLs
self-describing, followed by a URL-safe random token.

Per ``zero-control-plane-trust`` §"Identity is a link, not a name":
authority follows verified stable identifiers, not display names or
usernames. The ID is the only authority.
"""

from __future__ import annotations

import secrets
import string

# Use a 24-character token (130 bits of entropy) which is sufficient
# for unguessability while remaining short enough for logs and URLs.
_TOKEN_ALPHABET = string.ascii_lowercase + string.digits
_TOKEN_LENGTH = 24


def _generate_token() -> str:
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(_TOKEN_LENGTH))


def generate_user_id() -> str:
    return f"zu_{_generate_token()}"


def generate_project_id() -> str:
    return f"p_{_generate_token()}"


def generate_external_identity_id() -> str:
    return f"ei_{_generate_token()}"


def generate_secret_reference_id() -> str:
    return f"sec_{_generate_token()}"


def generate_tool_id() -> str:
    return f"tool_{_generate_token()}"


def generate_tool_grant_id() -> str:
    return f"tg_{_generate_token()}"


def generate_audit_event_id() -> str:
    return f"aud_{_generate_token()}"


def generate_correlation_id() -> str:
    return f"corr_{_generate_token()}"


# Phase 3 — Plan lifecycle (M4)
def generate_conversation_event_id() -> str:
    return f"evt_{_generate_token()}"


def generate_plan_id() -> str:
    return f"plan_{_generate_token()}"


def generate_plan_revision_id() -> str:
    return f"pr_{_generate_token()}"


def generate_plan_approval_id() -> str:
    return f"pa_{_generate_token()}"


def generate_plan_handoff_id() -> str:
    return f"ph_{_generate_token()}"


# Phase 3 — Execution graph (M5)
def generate_execution_id() -> str:
    return f"exec_{_generate_token()}"


def generate_task_id() -> str:
    return f"task_{_generate_token()}"


def generate_task_attempt_id() -> str:
    return f"att_{_generate_token()}"


def generate_execution_snapshot_id() -> str:
    return f"snap_{_generate_token()}"


# Phase 4 — Worktrees and repositories (M6)
def generate_repository_id() -> str:
    return f"repo_{_generate_token()}"


def generate_worktree_id() -> str:
    return f"wt_{_generate_token()}"


def generate_command_run_id() -> str:
    return f"cr_{_generate_token()}"


def generate_task_artifact_id() -> str:
    return f"art_{_generate_token()}"


# Phase 4 — Dynamic Sub Agent Types (M7)
def generate_agent_type_id() -> str:
    return f"at_{_generate_token()}"


def generate_agent_instance_id() -> str:
    return f"ai_{_generate_token()}"


def generate_knowledge_record_id() -> str:
    return f"kr_{_generate_token()}"


def generate_topology_snapshot_id() -> str:
    return f"topo_{_generate_token()}"


# Phase 5 — Artifacts, RAG, Context, Compaction (M8, M9)
def generate_artifact_id() -> str:
    return f"art_{_generate_token()}"


def generate_artifact_provenance_id() -> str:
    return f"ap_{_generate_token()}"


def generate_rag_document_id() -> str:
    return f"rag_{_generate_token()}"


def generate_context_version_id() -> str:
    return f"cv_{_generate_token()}"


def generate_injection_ledger_id() -> str:
    return f"il_{_generate_token()}"


def generate_compaction_record_id() -> str:
    return f"comp_{_generate_token()}"


# Phase 6 — Provider Adapters and Integration (M10, M11)
def generate_provider_model_id() -> str:
    return f"pm_{_generate_token()}"


def generate_provider_request_id() -> str:
    return f"preq_{_generate_token()}"


def generate_usage_record_id() -> str:
    return f"usg_{_generate_token()}"


def generate_pricing_entry_id() -> str:
    return f"price_{_generate_token()}"


def generate_integration_review_id() -> str:
    return f"irev_{_generate_token()}"


def generate_merge_proposal_id() -> str:
    return f"mp_{_generate_token()}"


def generate_integration_worktree_id() -> str:
    return f"iwt_{_generate_token()}"


def generate_integration_evidence_id() -> str:
    return f"ime_{_generate_token()}"


def generate_integration_test_evidence_id() -> str:
    return f"ite_{_generate_token()}"


# Phase 8 — Interface Adapters (M13)
def generate_interface_binding_id() -> str:
    return f"ib_{_generate_token()}"


def generate_interface_event_id() -> str:
    return f"iev_{_generate_token()}"


def generate_interface_delivery_id() -> str:
    return f"idl_{_generate_token()}"


def generate_callback_token_id() -> str:
    return f"ct_{_generate_token()}"


def generate_tool_approval_id() -> str:
    return f"ta_{_generate_token()}"
