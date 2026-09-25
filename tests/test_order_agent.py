from __future__ import annotations

import asyncio
from typing import Any

import pytest

from student_agent.order_agent import investigate_order


class FakeGateway:
    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses

    async def call(self, tool_name: str, **_: str) -> dict[str, Any]:
        return self.responses[tool_name]


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> dict[str, Any]:
        self.events.append(event)
        return event


def test_investigate_order_verifies_related_ids_and_evidence() -> None:
    responses = {
        "get_order": {
            "domain": "order",
            "evidence_ref": "ev_order_00000000000000000000",
            "data": {"order_id": "order-1", "order_status": "shipped"},
        },
        "get_order_items": {
            "domain": "item",
            "evidence_ref": "ev_items_00000000000000000000",
            "data": {
                "order_id": "order-1",
                "items": [
                    {"item_id": "item-1", "seller_id": "seller-1"},
                    {"order_item_id": 2, "seller_id": "seller-2"},
                ],
            },
        },
        "get_sellers": {
            "domain": "seller",
            "evidence_ref": "ev_sellers_00000000000000000000",
            "data": {
                "order_id": "order-1",
                "sellers": [{"seller_id": "seller-1"}, {"seller_id": "seller-2"}],
            },
        },
    }
    trace = FakeTrace()
    result = asyncio.run(
        investigate_order(
            {
                "case_id": "CASE_001",
                "customer_request": {"claimed_order_id": "order-1"},
            },
            FakeGateway(responses),  # type: ignore[arg-type]
            trace,  # type: ignore[arg-type]
        )
    )

    assert result["verification_status"] == "verified"
    assert result["order_ids"] == ["order-1"]
    assert result["item_ids"] == ["item-1", "2"]
    assert result["seller_ids"] == ["seller-1", "seller-2"]
    assert len(result["evidence_refs"]) == 3
    assert [event["event_type"] for event in trace.events] == [
        "tool_result_consumed",
        "tool_result_consumed",
        "tool_result_consumed",
        "handoff",
    ]


def test_investigate_order_rejects_missing_claimed_order_id() -> None:
    with pytest.raises(ValueError, match="claimed_order_id"):
        asyncio.run(
            investigate_order(
                {"case_id": "CASE_001", "customer_request": {}},
                FakeGateway({}),  # type: ignore[arg-type]
                FakeTrace(),  # type: ignore[arg-type]
            )
        )
