"""Route-surface characterization for the HTTP API.

Pins every (method, path) pair ``create_app`` registers so the planned
split of ``api.py`` into per-domain router modules cannot silently drop,
duplicate, or re-path a single endpoint. This is a refactor safety net:
it is expected to pass unchanged before, during, and after the split;
any diff here means public behavior changed and the split went wrong.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

# Golden table captured from the un-split monolith at commit 6378ead
# (93 pairs) + wave-14 fixes 17/21 (planner/propose, task reconcile):
# 95 distinct method+path pairs; see evidence/route-table-golden.json.
GOLDEN_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("DELETE", "/auth/tokens/current"),
        ("DELETE", "/projects/{project_id}/members/{user_id}"),
        ("DELETE", "/projects/{project_id}/tool-grants/{tool_id}"),
        ("GET", "/"),
        ("GET", "/capabilities"),
        ("GET", "/docs"),
        ("GET", "/docs/oauth2-redirect"),
        ("GET", "/healthz"),
        ("GET", "/metrics"),
        ("GET", "/openapi.json"),
        ("GET", "/projects/{project_id}"),
        ("GET", "/projects/{project_id}/agent-types"),
        ("GET", "/projects/{project_id}/agent-types/{type_id}"),
        ("GET", "/projects/{project_id}/agent-types/{type_id}/knowledge"),
        ("GET", "/projects/{project_id}/artifacts"),
        ("GET", "/projects/{project_id}/artifacts/{artifact_id}"),
        ("GET", "/projects/{project_id}/audit"),
        ("GET", "/projects/{project_id}/executions/{execution_id}"),
        ("GET", "/projects/{project_id}/executions/{execution_id}/ready-tasks"),
        ("GET", "/projects/{project_id}/executions/{execution_id}/tasks"),
        ("GET", "/projects/{project_id}/integration"),
        ("GET", "/projects/{project_id}/integration/proposals"),
        ("GET", "/projects/{project_id}/integration/proposals/{proposal_id}"),
        ("GET", "/projects/{project_id}/integration/reviews"),
        ("GET", "/projects/{project_id}/integration/reviews/{review_id}"),
        ("GET", "/projects/{project_id}/interfaces"),
        ("GET", "/projects/{project_id}/interfaces/events"),
        ("GET", "/projects/{project_id}/members"),
        ("GET", "/projects/{project_id}/plans"),
        ("GET", "/projects/{project_id}/plans/{plan_id}/revisions"),
        ("GET", "/projects/{project_id}/providers"),
        ("GET", "/projects/{project_id}/providers/requests"),
        ("GET", "/projects/{project_id}/providers/requests/unknown"),
        ("GET", "/projects/{project_id}/providers/usage"),
        ("GET", "/projects/{project_id}/rag"),
        ("GET", "/projects/{project_id}/rag/{doc_id}"),
        ("GET", "/projects/{project_id}/repositories"),
        ("GET", "/projects/{project_id}/result-deliveries"),
        ("GET", "/projects/{project_id}/secrets"),
        ("GET", "/projects/{project_id}/topology"),
        ("GET", "/projects/{project_id}/worktrees"),
        ("GET", "/projects/{project_id}/worktrees/{worktree_id}"),
        ("GET", "/providers"),
        ("GET", "/providers/{provider}/{model_name}"),
        ("GET", "/readyz"),
        ("GET", "/redoc"),
        ("GET", "/tools"),
        ("GET", "/users/{user_id}"),
        ("POST", "/auth/bootstrap"),
        ("POST", "/auth/tokens"),
        ("POST", "/projects"),
        ("POST", "/projects/{project_id}/agent-types"),
        ("POST", "/projects/{project_id}/agent-types/{type_id}/knowledge"),
        ("POST", "/projects/{project_id}/artifacts"),
        ("POST", "/projects/{project_id}/authorize"),
        ("POST", "/projects/{project_id}/conversation-events"),
        ("POST", "/projects/{project_id}/executions/{execution_id}/cancel"),
        ("POST", "/projects/{project_id}/executions/{execution_id}/recover"),
        ("POST", "/projects/{project_id}/executions/{execution_id}/run-ready"),
        ("POST", "/projects/{project_id}/handoffs/{handoff_id}/executions"),
        ("POST", "/projects/{project_id}/executions/{execution_id}/tasks/{task_id}/reconcile"),
        ("POST", "/projects/{project_id}/integration/proposals"),
        ("POST", "/projects/{project_id}/integration/proposals/{proposal_id}/approve"),
        ("POST", "/projects/{project_id}/integration/proposals/{proposal_id}/execute"),
        ("POST", "/projects/{project_id}/integration/proposals/{proposal_id}/reject"),
        ("POST", "/projects/{project_id}/integration/reviews"),
        ("POST", "/projects/{project_id}/integration/reviews/{review_id}/combined-test"),
        ("POST", "/projects/{project_id}/interfaces"),
        ("POST", "/projects/{project_id}/interfaces/{binding_id}/disable"),
        ("POST", "/projects/{project_id}/interfaces/{binding_id}/enable"),
        ("POST", "/projects/{project_id}/members"),
        ("POST", "/projects/{project_id}/plans"),
        ("POST", "/projects/{project_id}/plans/{plan_id}/approve"),
        ("POST", "/projects/{project_id}/plans/{plan_id}/reject"),
        ("POST", "/projects/{project_id}/plans/{plan_id}/revisions"),
        ("POST", "/projects/{project_id}/planner/propose"),
        ("POST", "/projects/{project_id}/providers/requests/{request_id}/reconcile"),
        ("GET", "/projects/{project_id}/tool-approvals"),
        ("POST", "/projects/{project_id}/rag"),
        ("POST", "/projects/{project_id}/rag/rebuild"),
        ("POST", "/projects/{project_id}/rag/search"),
        ("POST", "/projects/{project_id}/repositories"),
        ("POST", "/projects/{project_id}/result-deliveries/drain"),
        ("POST", "/projects/{project_id}/scheduler/tick"),
        ("POST", "/projects/{project_id}/secrets"),
        ("POST", "/projects/{project_id}/secrets/{secret_id}/revoke"),
        ("POST", "/projects/{project_id}/tool-grants"),
        ("POST", "/projects/{project_id}/tool-invocations"),
        ("POST", "/users"),
        ("POST", "/users/{user_id}/external-identities"),
        ("POST", "/users/{user_id}/external-identities/verify"),
        ("POST", "/webhooks/{platform}/{project_id}/{binding_id}"),
        ("POST", "/projects/{project_id}/tool-approvals/{request_id}/resolve"),
    }
)


# Schema-only operations: the HTML management surface (zero.web.controller)
# reaches OpenAPI through an include wrapper that hides its routes from
# app.routes, so this second golden set pins it independently.
GOLDEN_WEB_SCHEMA_OPS: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/web/"),
        ("GET", "/web/audit"),
        ("GET", "/web/login"),
        ("GET", "/web/projects"),
        ("GET", "/web/projects/{project_id}"),
        ("GET", "/web/projects/{project_id}/executions/{execution_id}"),
        ("GET", "/web/projects/{project_id}/plans/{plan_id}"),
        ("GET", "/web/users"),
        ("POST", "/web/login"),
        ("POST", "/web/logout"),
        ("POST", "/web/projects"),
        ("POST", "/web/projects/{project_id}/executions/{execution_id}/cancel"),
        ("POST", "/web/projects/{project_id}/executions/{execution_id}/recover"),
        ("POST", "/web/projects/{project_id}/members"),
        ("POST", "/web/projects/{project_id}/plans"),
        ("POST", "/web/projects/{project_id}/plans/{plan_id}/approve"),
        ("POST", "/web/projects/{project_id}/plans/{plan_id}/reject"),
        ("POST", "/web/projects/{project_id}/plans/{plan_id}/revisions"),
        ("POST", "/web/users"),
    }
)


def _registered_methods_and_paths(
    app: FastAPI,
) -> tuple[frozenset[tuple[str, str]], list[tuple[str, str]]]:
    """Return (distinct pairs, full list) of every registered route."""
    pairs: list[tuple[str, str]] = []
    for route in app.routes:
        for method in getattr(route, "methods", ()) or ():
            # HEAD/OPTIONS are added automatically by Starlette for GET
            # routes; they are implied behavior, not declared surface.
            if method in {"HEAD", "OPTIONS"}:
                continue
            pairs.append((method, route.path))
    return frozenset(pairs), pairs


@pytest.mark.usefixtures("app")
class TestApiRouteSurface:
    def test_every_declared_endpoint_matches_the_golden_table(self, app: FastAPI) -> None:
        distinct, _ = _registered_methods_and_paths(app)
        missing = sorted(GOLDEN_ROUTES - distinct)
        extra = sorted(distinct - GOLDEN_ROUTES)
        assert not missing, f"endpoints disappeared: {missing}"
        assert not extra, f"unexpected endpoints appeared: {extra}"

    def test_no_endpoint_is_registered_twice(self, app: FastAPI) -> None:
        _, pairs = _registered_methods_and_paths(app)
        seen: set[tuple[str, str]] = set()
        duplicates = [pair for pair in pairs if pair in seen or seen.add(pair)]  # type: ignore[func-args]
        assert not duplicates, f"duplicate registrations: {duplicates}"

    def test_openapi_operations_match_both_golden_surfaces(self, app: FastAPI) -> None:
        # A bare route-table diff cannot see shadowing: if a later
        # registration swallows an earlier one, both still appear in
        # app.routes but only the survivor exists in the schema. The
        # schema must contain every JSON-API operation plus the full
        # management-web surface; FastAPI excludes only its own docs
        # endpoints (/docs, /redoc, /openapi.json) from self-reporting.
        schema = app.openapi()
        operations = frozenset(
            (method.upper(), path)
            for path, path_item in schema.get("paths", {}).items()
            for method in path_item
            if method in {"get", "post", "put", "patch", "delete"}
        )
        docs_self_excluded = {
            ("GET", "/docs"),
            ("GET", "/docs/oauth2-redirect"),
            ("GET", "/openapi.json"),
            ("GET", "/redoc"),
        }
        expected = (GOLDEN_ROUTES - docs_self_excluded) | GOLDEN_WEB_SCHEMA_OPS
        missing = sorted(expected - operations)
        extra = sorted(operations - expected)
        assert not missing, f"operations missing from schema: {missing}"
        assert not extra, f"unexpected schema operations: {extra}"
