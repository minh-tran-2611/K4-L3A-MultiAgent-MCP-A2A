from __future__ import annotations

import asyncio
from typing import Any

from student_agent.payment_agent import analyze_payment


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


def test_payment_agent_keeps_payment_references_and_evidence() -> None:
    responses = {
        "get_order_payments": {
            "domain": "payment",
            "evidence_ref": "ev_payment_12345678901234567890",
            "data": {
                "payments": [
                    {"payment_sequential": 1, "payment_value": 40.0},
                    {"transaction_id": "tx-2", "payment_value": 40.0},
                ]
            },
        },
        "get_payment_timeline": {
            "domain": "payment",
            "evidence_ref": "ev_timeline_1234567890123456789",
            "data": {"events": []},
        },
        "get_refund_timeline": {
            "domain": "refund",
            "evidence_ref": "ev_refund_12345678901234567890",
            "data": {"events": []},
        },
    }
    trace = FakeTrace()

    result = asyncio.run(
        analyze_payment(
            {
                "case_id": "L3A_CASE_001",
                "customer_request": {"claimed_order_id": "order-1"},
            },
            FakeGateway(responses),  # type: ignore[arg-type]
            trace,  # type: ignore[arg-type]
        )
    )

    assert result["status"] == "completed"
    assert result["entities"]["payment_references"] == ["1", "tx-2"]
    assert len(result["evidence_refs"]) == 3
    assert [event["event_type"] for event in trace.events] == [
        "tool_result_consumed",
        "tool_result_consumed",
        "tool_result_consumed",
        "handoff",
    ]


def test_payment_agent_reports_missing_evidence() -> None:
    unavailable = RuntimeError("unavailable")
    trace = FakeTrace()

    result = asyncio.run(
        analyze_payment(
            {
                "case_id": "L3A_CASE_001",
                "customer_request": {"claimed_order_id": "order-1"},
            },
            FakeGateway(
                {
                    "get_order_payments": unavailable,
                    "get_payment_timeline": unavailable,
                    "get_refund_timeline": unavailable,
                }
            ),  # type: ignore[arg-type]
            trace,  # type: ignore[arg-type]
        )
    )

    assert result["status"] == "partial"
    assert result["entities"]["payment_references"] == []
    assert result["evidence_refs"] == []
    assert {issue["code"] for issue in result["issues"]} >= {
        "PAYMENT_EVIDENCE_MISSING",
        "REFUND_EVIDENCE_MISSING",
    }
