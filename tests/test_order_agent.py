from __future__ import annotations

import asyncio
from typing import Any

import pytest

from student_agent.order_agent import analyze_order


class FakeGateway:
    def __init__(self, responses: dict[str, dict[str, Any] | Exception]) -> None:
        self.responses = responses

    async def call(self, tool_name: str, **_: str) -> dict[str, Any]:
        response = self.responses[tool_name]
        if isinstance(response, Exception):
            raise response
        return response


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> dict[str, Any]:
        self.events.append(event)
        return event


def test_analyze_order_verifies_related_ids_and_evidence() -> None:
    responses = {
        "get_order": {
            "domain": "order",
            "evidence_ref": "ev_order_00000000000000000000",
            "data": {"order_id": "order-1", "order_status": "canceled"},
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
        analyze_order(
            {
                "case_id": "CASE_001",
                "customer_request": {"claimed_order_id": "order-1"},
            },
            FakeGateway(responses),  # type: ignore[arg-type]
            trace,  # type: ignore[arg-type]
        )
    )

    assert result["case_id"] == "CASE_001"
    assert result["status"] == "completed"
    assert result["entities"] == {
        "order_ids": ["order-1"],
        "item_ids": ["item-1", "2"],
        "seller_ids": ["seller-1", "seller-2"],
    }
    assert [finding["code"] for finding in result["findings"]] == [
        "ORDER_CANCELED",
        "ORDER_ITEMS_CONFIRMED",
        "ORDER_SELLERS_CONFIRMED",
    ]
    assert all(finding["evidence_refs"] for finding in result["findings"])
    assert result["issues"] == []
    assert len(result["evidence_refs"]) == 3
    assert [event["event_type"] for event in trace.events] == [
        "tool_result_consumed",
        "tool_result_consumed",
        "tool_result_consumed",
        "handoff",
    ]


def test_analyze_order_reports_missing_and_conflicting_evidence() -> None:
    responses: dict[str, dict[str, Any] | Exception] = {
        "get_order": RuntimeError("order evidence unavailable"),
        "get_order_items": {
            "domain": "item",
            "evidence_ref": "ev_items_00000000000000000000",
            "data": {
                "order_id": "order-1",
                "items": [{"item_id": "item-1", "seller_id": "seller-from-item"}],
            },
        },
        "get_sellers": {
            "domain": "seller",
            "evidence_ref": "ev_sellers_00000000000000000000",
            "data": [{"order_id": "other-order", "seller_id": "seller-from-record"}],
        },
    }
    trace = FakeTrace()
    result = asyncio.run(
        analyze_order(
            {
                "case_id": "CASE_001",
                "customer_request": {"claimed_order_id": "order-1"},
            },
            FakeGateway(responses),  # type: ignore[arg-type]
            trace,  # type: ignore[arg-type]
        )
    )

    assert result["status"] == "partial"
    assert {issue["code"] for issue in result["issues"]} == {
        "TOOL_ERROR",
        "ORDER_ID_MISMATCH",
        "SELLER_ID_MISMATCH",
    }
    assert all(finding["code"] != "ORDER_STATUS_CONFIRMED" for finding in result["findings"])
    assert len(result["evidence_refs"]) == 2
    assert [event["event_type"] for event in trace.events] == [
        "tool_result_consumed",
        "tool_result_consumed",
        "handoff",
    ]


def test_analyze_order_rejects_missing_claimed_order_id() -> None:
    with pytest.raises(ValueError, match="claimed_order_id"):
        asyncio.run(
            analyze_order(
                {"case_id": "CASE_001", "customer_request": {}},
                FakeGateway({}),  # type: ignore[arg-type]
                FakeTrace(),  # type: ignore[arg-type]
            )
        )
