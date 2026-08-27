"""Regression: interface event API payloads keep the event_content field.

Live group runs (Task 7/8) consume ``GET /projects/{id}/interfaces/events``
to classify TASK vs NORMAL ingestions; dropping ``event_content`` from
the payload silently blinded that pipeline (BUG-C). These tests pin the
contract for message and callback entries alike.
"""

from __future__ import annotations

from zero.app.interface_service import InterfaceEventLogEntry
from zero.app.routers.interface import _interface_event_payload
from zero.domain.identity import ProjectId, UserId
from zero.domain.interfaces import InterfaceEventId


def _entry(**overrides) -> InterfaceEventLogEntry:
    fields = {
        "id": InterfaceEventId("iev_test000000000000000000000001"),
        "project_id": ProjectId("p_test000000000000000000000001"),
        "platform": "telegram",
        "external_event_id": "9001",
        "external_actor_id": "424242",
        "resolved_user_id": UserId("zu_test00000000000000000000001"),
        "chat_id": "-1004406039396",
        "topic_id": None,
        "event_kind": "message",
        "event_content": "زیرو داشبورد فروش بساز",
        "processing_result": "processed",
        "processing_detail": "ingested as conversation event ce_1; proposed revision pr_1",
        "created_at": "2026-08-27T00:00:00.000Z",
    }
    fields.update(overrides)
    return InterfaceEventLogEntry(**fields)


def test_payload_includes_event_content_and_stable_contract():
    payload = _interface_event_payload(_entry())
    assert payload["event_content"] == "زیرو داشبورد فروش بساز"
    assert set(payload) == {
        "id",
        "project_id",
        "platform",
        "external_event_id",
        "external_actor_id",
        "resolved_user_id",
        "chat_id",
        "topic_id",
        "event_kind",
        "event_content",
        "processing_result",
        "processing_detail",
        "created_at",
    }


def test_payload_tolerates_entries_without_content_attribute():
    class _Sparse:
        id = InterfaceEventId("iev_sparse00000000000000000000001")
        project_id = None
        platform = "telegram"
        external_event_id = "7"
        external_actor_id = "99"
        resolved_user_id = None
        chat_id = "-100"
        topic_id = None
        event_kind = "other"
        processing_result = None
        processing_detail = None
        created_at = None

    payload = _interface_event_payload(_Sparse())
    assert payload["event_content"] is None
    assert payload["processing_result"] is None
