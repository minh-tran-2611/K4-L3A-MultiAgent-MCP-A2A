from __future__ import annotations

import asyncio
from typing import Any

from student_agent.payment_agent import analyze_payment

CASE = {
    "case_id": "L3A_CASE_005",
    "customer_request": {"claimed_order_id": "order-5", "claims": [
        {"claim_id": "claim-5-a", "topic": "valid_split_payment"},
        {"claim_id": "claim-5-b", "topic": "requested_full_refund"},
    ]},
}
PAYMENT_REF = "ev_payment_12345678901234567890"
TIMELINE_REF = "ev_timeline_12345678901234567"
REFUND_REF = "ev_refund_123456789012345678901"


def evidence(domain: str, ref: str, data: Any) -> dict[str, Any]:
    return {"domain": domain, "evidence_ref": ref, "data": data}


class FakeGateway:
    def __init__(self, responses: dict[str, dict[str, Any]]):
        self.responses = responses
        self.calls: list[tuple[str, str, str]] = []

    async def call(self, tool_name: str, *, case_id: str, order_id: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, order_id))
        return self.responses[tool_name]


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> None:
        self.events.append(event)


def test_split_payment_and_completed_refund_handoff_has_evidence() -> None:
    gateway = FakeGateway({
        "get_order_payments": evidence("payment", PAYMENT_REF, [
            {"order_id": "order-5", "payment_sequential": "1", "payment_value": "60.00"},
            {"order_id": "order-5", "payment_sequential": "2", "payment_value": "40.00"},
        ]),
        "get_payment_timeline": evidence("payment", TIMELINE_REF, {
            "order_id": "order-5", "events": [
                {"order_id": "order-5", "event_type": "captured", "status": "confirmed",
                 "amount_brl": "60.00"},
                {"order_id": "order-5", "event_type": "captured", "status": "confirmed",
                 "amount_brl": "40.00"},
            ],
        }),
        "get_refund_timeline": evidence("refund", REFUND_REF, {
            "order_id": "order-5", "events": [
                {"event_type": "refund_requested", "status": "completed",
                 "amount_brl": "20.00"},
            ],
        }),
    })
    trace = FakeTrace()

    result = asyncio.run(analyze_payment(CASE, gateway, trace))

    assert result["status"] == "completed"
    assert result["financials"]["recorded_payment_total_brl"] == 100.0
    assert result["financials"]["confirmed_capture_total_brl"] == 100.0
    assert result["financials"]["completed_refund_brl"] == 20.0
    assert result["financials"]["maximum_additional_refund_brl"] == 80.0
    assert result["financials"]["recommended_refund_brl"] is None
    assert result["financials"]["refund_lines"] == []
    assert result["evidence_refs"] == [PAYMENT_REF, TIMELINE_REF, REFUND_REF]
    assert [call[0] for call in gateway.calls] == [
        "get_order_payments", "get_payment_timeline", "get_refund_timeline"
    ]
    assert [event["event_type"] for event in trace.events] == [
        "tool_result_consumed", "tool_result_consumed", "tool_result_consumed", "handoff"
    ]


def test_pending_refund_reduces_additional_refund_limit() -> None:
    gateway = FakeGateway({
        "get_order_payments": evidence("payment", PAYMENT_REF, [
            {"order_id": "order-5", "payment_sequential": "1", "payment_value": "100.00"},
        ]),
        "get_payment_timeline": evidence("payment", TIMELINE_REF, {
            "events": [{"event_type": "captured", "status": "confirmed",
                        "amount_brl": "100.00"}],
        }),
        "get_refund_timeline": evidence("refund", REFUND_REF, {
            "events": [{"event_type": "refund_requested", "status": "pending",
                        "amount_brl": "35.00"}],
        }),
    })

    result = asyncio.run(analyze_payment(CASE, gateway, FakeTrace()))

    assert result["financials"]["pending_refund_brl"] == 35.0
    assert result["financials"]["maximum_additional_refund_brl"] == 65.0
    assert result["financials"]["recommended_refund_brl"] is None


def test_timeline_anomaly_is_handed_off_with_its_evidence() -> None:
    gateway = FakeGateway({
        "get_order_payments": evidence("payment", PAYMENT_REF, [
            {"payment_sequential": "1", "payment_value": "64.00"},
            {"payment_sequential": "2", "payment_value": "64.00"},
        ]),
        "get_payment_timeline": evidence("payment", TIMELINE_REF, {
            "events": [{"issue_code": "duplicate_charge"}],
        }),
        "get_refund_timeline": evidence("refund", REFUND_REF, {"events": []}),
    })

    result = asyncio.run(analyze_payment(CASE, gateway, FakeTrace()))

    assert any(finding.get("anomaly_code") == "duplicate_charge"
               and finding["evidence_refs"] == [TIMELINE_REF]
               for finding in result["findings"])
    assert "1" in result["entities"]["payment_references"]
    assert "2" in result["entities"]["payment_references"]


def test_multiple_refund_events_without_reference_are_not_summed() -> None:
    gateway = FakeGateway({
        "get_order_payments": evidence("payment", PAYMENT_REF, [
            {"payment_value": "100.00"},
        ]),
        "get_payment_timeline": evidence("payment", TIMELINE_REF, {"events": []}),
        "get_refund_timeline": evidence("refund", REFUND_REF, {"events": [
            {"event_type": "refund_requested", "status": "pending", "amount_brl": "40.00"},
            {"event_type": "refund_requested", "status": "completed", "amount_brl": "40.00"},
        ]}),
    })

    result = asyncio.run(analyze_payment(CASE, gateway, FakeTrace()))

    assert "REFUND_EVENTS_AMBIGUOUS" in {issue["code"] for issue in result["issues"]}
    assert "completed_refund_brl" not in result["financials"]
    assert "maximum_additional_refund_brl" not in result["financials"]


def test_refund_exceeding_recorded_payment_is_reported() -> None:
    gateway = FakeGateway({
        "get_order_payments": evidence("payment", PAYMENT_REF, [
            {"payment_value": "50.00"},
        ]),
        "get_payment_timeline": evidence("payment", TIMELINE_REF, {"events": []}),
        "get_refund_timeline": evidence("refund", REFUND_REF, {"events": [
            {"event_type": "refund_requested", "status": "completed", "amount_brl": "60.00"},
        ]}),
    })

    result = asyncio.run(analyze_payment(CASE, gateway, FakeTrace()))

    assert "REFUND_TOTAL_EXCEEDS_PAYMENT" in {issue["code"] for issue in result["issues"]}
    assert "maximum_additional_refund_brl" not in result["financials"]
