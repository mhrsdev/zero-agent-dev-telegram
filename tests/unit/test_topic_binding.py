"""Unit tests for zero.telegram.topic_binding — ADR T-4.4, T-4.5, T-4.6."""
from __future__ import annotations

import pytest
from zero.core.scope import Mode
from zero.telegram.topic_binding import (
    GroupPolicy,
    GroupPolicyStore,
    TopicBinding,
    TopicBindingStore,
    resolve_mode,
)


@pytest.fixture
def binding_store() -> TopicBindingStore:
    return TopicBindingStore()


@pytest.fixture
def policy_store() -> GroupPolicyStore:
    return GroupPolicyStore()


# ---------------------------------------------------------------------- TopicBinding invariants

class TestTopicBinding:
    def test_normal_binding_valid(self) -> None:
        b = TopicBinding(
            group_id="grp_01HABC",
            topic_id=100,
            mode="normal",
            memory_scope_id="mem:grp:grp_01HABC:100",
            configured_by="usr_01HALICE",
        )
        assert b.mode == "normal"
        assert b.project_id is None

    def test_disabled_binding_valid(self) -> None:
        b = TopicBinding(
            group_id="grp_01HABC",
            topic_id=100,
            mode="disabled",
            memory_scope_id="mem:grp:grp_01HABC:100",
            configured_by="usr_01HALICE",
        )
        assert b.mode == "disabled"
        assert b.project_id is None

    def test_dev_binding_requires_project_id(self) -> None:
        with pytest.raises(ValueError, match="requires project_id"):
            TopicBinding(
                group_id="grp_01HABC",
                topic_id=100,
                mode="dev",
                memory_scope_id="mem:prj:prj_01HABC",
                configured_by="usr_01HALICE",
                # project_id=None
            )

    def test_normal_binding_forbids_project_id(self) -> None:
        with pytest.raises(ValueError, match="forbids project_id"):
            TopicBinding(
                group_id="grp_01HABC",
                topic_id=100,
                mode="normal",
                memory_scope_id="mem:grp:grp_01HABC:100",
                configured_by="usr_01HALICE",
                project_id="prj_01HABC",
            )

    def test_invalid_mode_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be normal/dev/disabled"):
            TopicBinding(
                group_id="grp_01HABC",
                topic_id=100,
                mode="personal",  # personal is NOT a valid TopicBinding.mode
                memory_scope_id="mem:foo",
                configured_by="usr_01HALICE",
            )

    def test_dev_binding_with_project_id_ok(self) -> None:
        b = TopicBinding(
            group_id="grp_01HABC",
            topic_id=100,
            mode="dev",
            memory_scope_id="mem:prj:prj_01HABC",
            configured_by="usr_01HALICE",
            project_id="prj_01HABC",
        )
        assert b.project_id == "prj_01HABC"

    def test_archive(self, binding_store: TopicBindingStore) -> None:
        b = TopicBinding(
            group_id="grp_01HABC", topic_id=100, mode="normal",
            memory_scope_id="mem:foo", configured_by="usr_01HALICE",
        )
        binding_store.upsert(b)
        assert binding_store.archive("grp_01HABC", 100) is True
        # Archived binding not returned by get()
        assert binding_store.get("grp_01HABC", 100) is None


# ---------------------------------------------------------------------- mode resolution

class TestResolveMode:
    def test_private_chat_always_personal(
        self, binding_store: TopicBindingStore, policy_store: GroupPolicyStore
    ) -> None:
        """T-4.6: private chat always PERSONAL."""
        result = resolve_mode(
            is_private=True,
            user_id="usr_01HALICE",
            binding_store=binding_store,
            policy_store=policy_store,
        )
        assert result.mode is Mode.PERSONAL
        assert result.silenced is False
        assert result.binding is None

    def test_group_with_normal_binding(
        self, binding_store: TopicBindingStore, policy_store: GroupPolicyStore
    ) -> None:
        b = TopicBinding(
            group_id="grp_01HABC", topic_id=100, mode="normal",
            memory_scope_id="mem:grp:grp_01HABC:100",
            configured_by="usr_01HALICE",
        )
        binding_store.upsert(b)
        result = resolve_mode(
            is_private=False,
            user_id="usr_01HALICE",
            group_id="grp_01HABC",
            topic_id=100,
            binding_store=binding_store,
            policy_store=policy_store,
        )
        assert result.mode is Mode.NORMAL
        assert result.binding is not None
        assert result.policy_applied is False

    def test_group_with_dev_binding(
        self, binding_store: TopicBindingStore, policy_store: GroupPolicyStore
    ) -> None:
        b = TopicBinding(
            group_id="grp_01HABC", topic_id=100, mode="dev",
            memory_scope_id="mem:prj:prj_01HABC",
            configured_by="usr_01HALICE",
            project_id="prj_01HABC",
        )
        binding_store.upsert(b)
        result = resolve_mode(
            is_private=False,
            user_id="usr_01HALICE",
            group_id="grp_01HABC",
            topic_id=100,
            binding_store=binding_store,
            policy_store=policy_store,
        )
        assert result.mode is Mode.DEVELOPMENT
        assert result.scope.project_id == "prj_01HABC"

    def test_group_with_disabled_binding_silenced(
        self, binding_store: TopicBindingStore, policy_store: GroupPolicyStore
    ) -> None:
        """T-4.6: disabled mode = complete silence."""
        b = TopicBinding(
            group_id="grp_01HABC", topic_id=100, mode="disabled",
            memory_scope_id="mem:grp:grp_01HABC:100",
            configured_by="usr_01HALICE",
        )
        binding_store.upsert(b)
        result = resolve_mode(
            is_private=False,
            user_id="usr_01HALICE",
            group_id="grp_01HABC",
            topic_id=100,
            binding_store=binding_store,
            policy_store=policy_store,
        )
        assert result.silenced is True

    def test_unbound_topic_uses_policy_default_normal(
        self, binding_store: TopicBindingStore, policy_store: GroupPolicyStore
    ) -> None:
        """T-4.5: no binding → GroupPolicy.default_unconfigured_topic_mode."""
        result = resolve_mode(
            is_private=False,
            user_id="usr_01HALICE",
            group_id="grp_01HABC",
            topic_id=999,
            binding_store=binding_store,
            policy_store=policy_store,
        )
        assert result.mode is Mode.NORMAL
        assert result.binding is None
        assert result.policy_applied is True

    def test_unbound_topic_with_policy_disabled_silenced(
        self, binding_store: TopicBindingStore, policy_store: GroupPolicyStore
    ) -> None:
        policy_store.set(GroupPolicy(
            group_id="grp_01HABC",
            default_unconfigured_topic_mode="disabled",
        ))
        result = resolve_mode(
            is_private=False,
            user_id="usr_01HALICE",
            group_id="grp_01HABC",
            topic_id=999,
            binding_store=binding_store,
            policy_store=policy_store,
        )
        assert result.silenced is True
        assert result.policy_applied is True

    def test_explicit_binding_overrides_policy(
        self, binding_store: TopicBindingStore, policy_store: GroupPolicyStore
    ) -> None:
        """T-4.5: explicit binding always wins over policy."""
        policy_store.set(GroupPolicy(
            group_id="grp_01HABC",
            default_unconfigured_topic_mode="disabled",
        ))
        # But we have an explicit 'normal' binding for topic 100
        b = TopicBinding(
            group_id="grp_01HABC", topic_id=100, mode="normal",
            memory_scope_id="mem:grp:grp_01HABC:100",
            configured_by="usr_01HALICE",
        )
        binding_store.upsert(b)
        result = resolve_mode(
            is_private=False,
            user_id="usr_01HALICE",
            group_id="grp_01HABC",
            topic_id=100,
            binding_store=binding_store,
            policy_store=policy_store,
        )
        assert result.mode is Mode.NORMAL
        assert result.silenced is False

    def test_non_forum_group_uses_topic_id_zero(
        self, binding_store: TopicBindingStore, policy_store: GroupPolicyStore
    ) -> None:
        """T-4.9: non-Forum groups use topic_id=0, treated like any other topic."""
        b = TopicBinding(
            group_id="grp_01HABC", topic_id=0, mode="normal",
            memory_scope_id="mem:grp:grp_01HABC:0",
            configured_by="usr_01HALICE",
        )
        binding_store.upsert(b)
        result = resolve_mode(
            is_private=False,
            user_id="usr_01HALICE",
            group_id="grp_01HABC",
            topic_id=0,
            binding_store=binding_store,
            policy_store=policy_store,
        )
        assert result.mode is Mode.NORMAL

    def test_private_chat_ignores_binding(
        self, binding_store: TopicBindingStore, policy_store: GroupPolicyStore
    ) -> None:
        """Private chat is always PERSONAL — even if a binding somehow exists."""
        result = resolve_mode(
            is_private=True,
            user_id="usr_01HALICE",
            # group_id and topic_id are None for private chat
            binding_store=binding_store,
            policy_store=policy_store,
        )
        assert result.mode is Mode.PERSONAL
