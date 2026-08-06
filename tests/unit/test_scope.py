"""Unit tests for zero.core.scope — ADR 0003 acceptance criteria.

These 12 acceptance criteria from ADR 0003 §5 are the mandatory tests for the
Scope type. Plus ≥10 adversarial tests.
"""
from __future__ import annotations

import pytest
from zero.core.scope import (
    PERSONAL_USER_ID_SENTINEL,
    Mode,
    Scope,
    ScopeError,
    ScopeKeyError,
)

# ---------------------------------------------------------------------- basic construction

def test_personal_scope_basic() -> None:
    """PERSONAL scope with user_id is valid."""
    s = Scope.personal(user_id="usr_01HABCDEF")
    assert s.mode is Mode.PERSONAL
    assert s.user_id == "usr_01HABCDEF"
    assert s.is_personal()
    assert s.allows_personal_memory()


def test_normal_scope_basic() -> None:
    """NORMAL scope with group_id + topic_id is valid."""
    s = Scope.normal(group_id="grp_01HABC", topic_id=100)
    assert s.mode is Mode.NORMAL
    assert s.group_id == "grp_01HABC"
    assert s.topic_id == 100
    assert s.is_normal()
    assert not s.allows_dev_features()


def test_development_scope_basic() -> None:
    """DEVELOPMENT scope with all five keys is valid."""
    s = Scope.development(
        org_id="org_01HABC",
        workspace_id="ws_01HABC",
        project_id="prj_01HABC",
        group_id="grp_01HABC",
        topic_id=200,
    )
    assert s.mode is Mode.DEVELOPMENT
    assert s.project_id == "prj_01HABC"
    assert s.is_development()
    assert s.allows_dev_features()


# ---------------------------------------------------------------------- 12 acceptance criteria (ADR 0003 §5)

class TestAcceptanceCriteria:
    """The 12 mandatory acceptance criteria from ADR 0003 §5."""

    def test_ac1_mode_is_first_field_and_immutable(self) -> None:
        """AC1: mode is the first field and immutable (frozen dataclass)."""
        s = Scope.personal(user_id="usr_01HABC")
        # Frozen — cannot reassign.
        with pytest.raises((AttributeError, TypeError)):
            s.mode = Mode.NORMAL  # type: ignore[misc]

    def test_ac2_exactly_one_key_group(self) -> None:
        """AC2: cannot have more than one key group populated."""
        with pytest.raises(ScopeKeyError):
            Scope(
                mode=Mode.PERSONAL,
                user_id="usr_01HABC",
                group_id="grp_01HABC",
                topic_id=100,
            )

    def test_ac3_no_scope_without_mode(self) -> None:
        """AC3: every Scope has a non-null mode."""
        # Cannot construct without mode — dataclass requires it.
        with pytest.raises(TypeError):
            Scope()  # type: ignore[call-arg]

    def test_ac4_mode_consistency_personal(self) -> None:
        """AC4: PERSONAL mode requires user_id and forbids group/topic."""
        with pytest.raises(ScopeKeyError):
            Scope(mode=Mode.PERSONAL, group_id="grp_01HABC", topic_id=100)

    def test_ac5_mode_consistency_normal(self) -> None:
        """AC5: NORMAL mode requires group_id + topic_id, forbids org/workspace/project."""
        with pytest.raises(ScopeKeyError):
            Scope(mode=Mode.NORMAL, group_id="grp_01HABC", topic_id=100, project_id="prj_01HABC")

    def test_ac6_mode_consistency_development(self) -> None:
        """AC6: DEVELOPMENT mode requires org+workspace+project+group+topic."""
        # Missing topic_id should fail.
        with pytest.raises(ScopeKeyError):
            Scope(
                mode=Mode.DEVELOPMENT,
                org_id="org_01HABC",
                workspace_id="ws_01HABC",
                project_id="prj_01HABC",
                group_id="grp_01HABC",
                # topic_id missing
            )
        # Missing org_id should fail.
        with pytest.raises(ScopeKeyError):
            Scope(
                mode=Mode.DEVELOPMENT,
                workspace_id="ws_01HABC",
                project_id="prj_01HABC",
                group_id="grp_01HABC",
                topic_id=200,
            )

    def test_ac7_normal_requires_both_group_and_topic(self) -> None:
        """AC7: NORMAL mode requires BOTH group_id AND topic_id (not just one)."""
        with pytest.raises(ScopeKeyError):
            Scope(mode=Mode.NORMAL, group_id="grp_01HABC")  # missing topic_id
        with pytest.raises(ScopeKeyError):
            Scope(mode=Mode.NORMAL, topic_id=100)  # missing group_id

    def test_ac8_id_prefix_validation(self) -> None:
        """AC8: ID prefixes are validated (usr_, grp_, org_, ws_, prj_)."""
        with pytest.raises(ScopeError):
            Scope.personal(user_id="wrong_prefix_01HABC")
        with pytest.raises(ScopeError):
            Scope.normal(group_id="badprefix_01HABC", topic_id=100)
        with pytest.raises(ScopeError):
            Scope.development(
                org_id="bad_org", workspace_id="ws_01HABC",
                project_id="prj_01HABC", group_id="grp_01HABC", topic_id=100,
            )

    def test_ac9_topic_id_non_negative_int(self) -> None:
        """AC9: topic_id is a non-negative int (0 allowed for non-Forum groups)."""
        # 0 is valid (non-Forum group sentinel)
        s = Scope.normal(group_id="grp_01HABC", topic_id=0)
        assert s.topic_id == 0
        # Negative is invalid
        with pytest.raises(ScopeError):
            Scope.normal(group_id="grp_01HABC", topic_id=-1)

    def test_ac10_memory_scope_auto_derived(self) -> None:
        """AC10: memory_scope_id is auto-derived from mode + keys."""
        s = Scope.personal(user_id="usr_01HABC").with_default_memory_scope()
        assert s.memory_scope_id == "mem:usr:usr_01HABC"

        s = Scope.normal(group_id="grp_01HABC", topic_id=100).with_default_memory_scope()
        assert s.memory_scope_id == "mem:grp:grp_01HABC:100"

        s = Scope.development(
            org_id="org_01HABC", workspace_id="ws_01HABC",
            project_id="prj_01HABC", group_id="grp_01HABC", topic_id=200,
        ).with_default_memory_scope()
        assert s.memory_scope_id == "mem:prj:prj_01HABC"

    def test_ac11_memory_scope_independent_of_mode(self) -> None:
        """AC11: memory_scope_id and mode are decoupled.

        A normal → dev mode change doesn't lose data because memory_scope_id
        is preserved (set explicitly).
        """
        s = Scope(
            mode=Mode.NORMAL,
            group_id="grp_01HABC",
            topic_id=100,
            memory_scope_id="mem:custom:custom_scope",
        )
        assert s.memory_scope_id == "mem:custom:custom_scope"

    def test_ac12_retrieval_key_stable_format(self) -> None:
        """AC12: retrieval_key produces stable format for DB indexing."""
        assert Scope.personal(user_id="usr_01HABC").retrieval_key() == "personal:usr_01HABC"
        assert (
            Scope.normal(group_id="grp_01HABC", topic_id=100).retrieval_key()
            == "normal:grp_01HABC:100"
        )
        assert (
            Scope.development(
                org_id="org_01HABC", workspace_id="ws_01HABC",
                project_id="prj_01HABC", group_id="grp_01HABC", topic_id=200,
            ).retrieval_key()
            == "dev:prj_01HABC"
        )


# ---------------------------------------------------------------------- adversarial tests (≥10)

class TestAdversarial:
    """Adversarial tests — deliberate attempts to break Scope invariants."""

    def test_adv1_personal_cannot_have_group(self) -> None:
        """PERSONAL mode must not carry group_id (would leak context)."""
        with pytest.raises(ScopeKeyError):
            Scope(mode=Mode.PERSONAL, user_id="usr_01HABC", group_id="grp_01HABC")

    def test_adv2_personal_cannot_have_topic(self) -> None:
        """PERSONAL mode must not carry topic_id."""
        with pytest.raises(ScopeKeyError):
            Scope(mode=Mode.PERSONAL, user_id="usr_01HABC", topic_id=100)

    def test_adv3_normal_cannot_have_user_id_as_key(self) -> None:
        """NORMAL mode: user_id is not a retrieval key (group memory is shared)."""
        # We allow user_id as metadata but it's stripped from .normal() factory.
        s = Scope.normal(group_id="grp_01HABC", topic_id=100, user_id="usr_01HABC")
        assert s.user_id is None  # stripped

    def test_adv4_normal_cannot_have_org(self) -> None:
        with pytest.raises(ScopeKeyError):
            Scope(mode=Mode.NORMAL, group_id="grp_01HABC", topic_id=100, org_id="org_01HABC")

    def test_adv5_dev_requires_all_five_keys(self) -> None:
        """DEVELOPMENT requires org+workspace+project+group+topic — all five."""
        with pytest.raises(ScopeKeyError):
            Scope.development(  # missing topic_id
                org_id="org_01HABC", workspace_id="ws_01HABC",
                project_id="prj_01HABC", group_id="grp_01HABC", topic_id=None,  # type: ignore[arg-type]
            )

    def test_adv6_dev_cannot_have_user_id(self) -> None:
        """DEVELOPMENT mode: user_id is metadata, not a key (per AC3)."""
        # This is allowed at construction (user_id is just metadata) but
        # shares_realm_with() ignores it for dev scope.
        s = Scope.development(
            org_id="org_01HABC", workspace_id="ws_01HABC",
            project_id="prj_01HABC", group_id="grp_01HABC", topic_id=100,
        )
        assert s.user_id is None  # not set in factory

    def test_adv7_invalid_mode_rejected(self) -> None:
        with pytest.raises(ScopeError):
            Scope(mode="invalid_mode", user_id="usr_01HABC")  # type: ignore[arg-type]

    def test_adv8_id_with_invalid_chars_rejected(self) -> None:
        """ID with spaces or special chars is rejected."""
        with pytest.raises(ScopeError):
            Scope.personal(user_id="usr_with spaces")

    def test_adv9_id_too_short_rejected(self) -> None:
        """Truncated ID (just prefix) is rejected."""
        with pytest.raises(ScopeError):
            Scope.personal(user_id="usr_")

    def test_adv10_topic_id_string_rejected(self) -> None:
        """topic_id must be int, not string."""
        with pytest.raises(ScopeError):
            Scope.normal(group_id="grp_01HABC", topic_id="100")  # type: ignore[arg-type]

    def test_adv11_scope_is_hashable(self) -> None:
        """Scope can be used as dict key (by retrieval_key)."""
        s1 = Scope.personal(user_id="usr_01HABC")
        s2 = Scope.personal(user_id="usr_01HABC")
        assert s1 == s2
        assert hash(s1) == hash(s2)
        d = {s1: "value"}
        assert d[s2] == "value"

    def test_adv12_scope_equality_ignores_memory_scope(self) -> None:
        """Two scopes with same mode + keys but different memory_scope_id are equal."""
        s1 = Scope.personal(user_id="usr_01HABC")
        s2 = Scope(
            mode=Mode.PERSONAL, user_id="usr_01HABC",
            memory_scope_id="mem:custom:foo",
        )
        assert s1 == s2  # equal for access checks

    def test_adv13_shares_realm_with_different_modes(self) -> None:
        """Scopes in different modes never share realm."""
        personal = Scope.personal(user_id="usr_01HABC")
        normal = Scope.normal(group_id="grp_01HABC", topic_id=100)
        assert not personal.shares_realm_with(normal)

    def test_adv14_to_log_dict_excludes_no_secrets(self) -> None:
        """to_log_dict() output never contains secrets (Scope doesn't hold any)."""
        s = Scope.personal(user_id="usr_01HABC")
        d = s.to_log_dict()
        # No secret-looking values.
        for v in d.values():
            if isinstance(v, str):
                assert not v.startswith("sk-")
                assert not v.startswith("ghp_")


# ---------------------------------------------------------------------- sentinel

def test_personal_user_id_sentinel_constant() -> None:
    """Sentinel is exposed for bootstrap paths."""
    assert PERSONAL_USER_ID_SENTINEL == "usr_bootstrap"
