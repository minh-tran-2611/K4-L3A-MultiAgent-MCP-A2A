from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.workflow import solve_case


class FakeGateway:
    def __init__(self, responses: dict[str, dict[str, Any] | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
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


def evidence(domain: str, index: int, data: Any) -> dict[str, Any]:
    return {"domain": domain, "evidence_ref": f"ev_{index:024}", "data": data}


CASE = {
    "case_id": "L3A_CASE_001",
    "customer_request": {
        "claimed_order_id": "order-1",
        "claims": [
            {"claim_id": "claim-a", "topic": "canceled_order_paid"},
            {"claim_id": "claim-b", "topic": "requested_full_refund"},
        ],
    },
    "policy_version": "EC_POLICY_V1",
}


def test_workflow_uses_case_scoped_evidence_and_verifies_output() -> None:
    gateway = FakeGateway(
        {
            "get_order": evidence("order", 1, {"order_id": "order-1", "order_status": "canceled"}),
            "get_order_items": evidence(
                "item",
                2,
                {"order_id": "order-1", "items": [{"item_id": "item-1", "seller_id": "seller-1"}]},
            ),
            "get_sellers": evidence(
                "seller", 3, {"order_id": "order-1", "sellers": [{"seller_id": "seller-1"}]}
            ),
            "get_order_payments": evidence(
                "payment",
                4,
                {
                    "order_id": "order-1",
                    "payments": [{"payment_reference": "pay-1", "payment_value": 80.0}],
                },
            ),
            "get_payment_timeline": evidence("payment", 5, {"order_id": "order-1"}),
            "get_refund_timeline": evidence(
                "refund", 6, {"order_id": "order-1", "status": "pending"}
            ),
            "get_shipment_summary": evidence(
                "shipment", 7, {"order_id": "order-1", "shipment_id": "ship-1"}
            ),
            "get_policy": evidence("policy", 8, {"version": "EC_POLICY_V1"}),
        }
    )
    trace = FakeTrace()

    output = asyncio.run(solve_case(CASE, gateway, trace))  # type: ignore[arg-type]
    root = Path(__file__).resolve().parents[1]
    Contracts(root / "contracts" / "schemas").validate_output(output, "test output")

    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["assessment"]["case_status"] == "action_required"
    assert output["claim_assessments"][0]["verdict"] == "supported"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert output["affected_entities"]["payment_references"] == ["pay-1"]
    assert output["affected_entities"]["shipment_ids"] == ["ship-1"]
    assert all(case_id == CASE["case_id"] for _, case_id, _ in gateway.calls)
    assert [event["event_type"] for event in trace.events].count("task_assigned") == 3
    assert [event["event_type"] for event in trace.events].count("handoff") == 3
    assert trace.events[-1]["event_type"] == "verification_completed"


def test_workflow_refuses_to_finalize_without_evidence() -> None:
    gateway = FakeGateway(
        {
            name: RuntimeError("unavailable")
            for name in (
                "get_order",
                "get_order_items",
                "get_sellers",
                "get_order_payments",
                "get_payment_timeline",
                "get_refund_timeline",
                "get_shipment_summary",
                "get_policy",
            )
        }
    )
    trace = FakeTrace()

    with pytest.raises(RuntimeError, match="no authoritative evidence"):
        asyncio.run(solve_case(CASE, gateway, trace))  # type: ignore[arg-type]

    assert all(event["event_type"] != "verification_completed" for event in trace.events)
