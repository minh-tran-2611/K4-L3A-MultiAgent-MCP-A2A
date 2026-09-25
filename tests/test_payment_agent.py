from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from student_agent.mcp_gateway import EvidenceGateway
from student_agent.payment_agent import analyze_payment

CASE = {
    "case_id": "L3A_CASE_005",
    "customer_request": {"claimed_order_id": "order-5", "claims": [
        {"claim_id": "claim-5-a", "topic": "valid_split_payment"},
        {"claim_id": "claim-5-b", "topic": "requested_full_refund"},
    ]},
}
PAYMENT_REF = "ev_payment_12345678901234567890"
REFUND_REF = "ev_refund_123456789012345678901"
ITEM_REF = "ev_item_123456789012345678901234"


class FakeGateway:
    def __init__(self, responses: dict[str, dict[str, Any]], tools: list[str] | None = None):
        self.responses = responses
        self.tools = tools if tools is not None else list(responses)
        self.calls: list[tuple[str, str, str]] = []

    async def list_tools(self) -> list[str]:
        return self.tools

    async def call(self, tool_name: str, *, case_id: str, order_id: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, order_id))
        return self.responses[tool_name]


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> None:
        self.events.append(event)


def evidence(domain: str, ref: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"domain": domain, "evidence_ref": ref, "data": data}


def test_split_payment_and_completed_refund_use_only_mcp_evidence() -> None:
    gateway = FakeGateway({
        "get_payments": evidence("payment", PAYMENT_REF, {
            "order_id": "order-5", "order_total_brl": "100.00",
            "payments": [
                {"payment_reference": "pay-a", "amount_brl": "60.00"},
                {"payment_reference": "pay-b", "amount_brl": "40.00"},
            ],
        }),
        "get_refunds": evidence("refund", REFUND_REF, {
            "order_id": "order-5", "refunds": [
                {"refund_reference": "refund-a", "amount_brl": "20.00",
                 "status": "completed"},
            ],
        }),
    })
    trace = FakeTrace()

    result = asyncio.run(analyze_payment(CASE, gateway, trace))

    assert result["case_id"] == CASE["case_id"]
    assert result["entities"]["payment_references"] == ["pay-a", "pay-b"]
    assert result["financials"]["recorded_payment_total_brl"] == 100.0
    assert result["financials"]["refund_totals_by_status_brl"] == {"completed": 20.0}
    assert result["financials"]["maximum_additional_refund_brl"] == 80.0
    assert result["financials"]["payment_lines"] == [
        {"payment_reference": "pay-a", "payment_sequential": None,
         "amount_brl": 60.0, "status": None,
         "evidence_refs": [PAYMENT_REF]},
        {"payment_reference": "pay-b", "payment_sequential": None,
         "amount_brl": 40.0, "status": None,
         "evidence_refs": [PAYMENT_REF]},
    ]
    assert result["financials"]["verified_refund_ledger_lines"] == [
        {"refund_reference": "refund-a", "payment_reference": None,
         "amount_brl": 20.0, "status": "completed", "evidence_refs": [REFUND_REF]},
    ]
    assert result["financials"]["recommended_refund_brl"] is None
    assert result["financials"]["refund_lines"] == []
    assert result["financials"]["completed_refund_brl"] == 20.0
    assert result["financials"]["maximum_additional_refund_brl"] == 80.0
    assert any(f["topic"] == "payment_reconciliation" and f["status"] == "matched"
               for f in result["findings"])
    assert any(f["topic"] == "valid_split_payment" and f["status"] == "supported"
               for f in result["findings"])
    assert result["evidence_refs"] == [PAYMENT_REF, REFUND_REF]
    assert gateway.calls == [
        ("get_payments", CASE["case_id"], "order-5"),
        ("get_refunds", CASE["case_id"], "order-5"),
    ]
    assert [event["event_type"] for event in trace.events] == [
        "tool_result_consumed", "tool_result_consumed", "handoff"
    ]


def test_mismatch_and_pending_refund_do_not_create_new_refund() -> None:
    gateway = FakeGateway({
        "get_payments": evidence("payment", PAYMENT_REF, {
            "order_id": "order-5", "order_total_brl": "80.00",
            "payments": [{"payment_reference": "pay-a", "amount_brl": "100.00"}],
        }),
        "get_refunds": evidence("refund", REFUND_REF, {
            "order_id": "order-5", "refunds": [
                {"amount_brl": "20.00", "status": "pending"},
            ],
        }),
    })
    result = asyncio.run(analyze_payment(CASE, gateway, FakeTrace()))

    assert any(f["topic"] == "payment_reconciliation" and f["status"] == "mismatch"
               and f["difference_brl"] == 20.0 for f in result["findings"])
    assert any(f["topic"] == "refund_status" and f["status"] == "pending"
               for f in result["findings"])
    assert result["financials"]["recommended_refund_brl"] is None
    assert result["financials"]["refund_lines"] == []
    assert result["financials"]["pending_refund_brl"] == 20.0
    assert result["financials"]["maximum_additional_refund_brl"] == 80.0


def test_cross_order_or_missing_evidence_is_reported_without_findings() -> None:
    gateway = FakeGateway({
        "get_payments": evidence("payment", PAYMENT_REF, {
            "order_id": "another-order", "payments": [
                {"payment_reference": "pay-other", "amount_brl": "50.00"},
            ],
        }),
    })
    trace = FakeTrace()
    result = asyncio.run(analyze_payment(CASE, gateway, trace))

    assert result["findings"] == []
    assert result["evidence_refs"] == []
    assert "order_id_conflict:payment" in result["issues"]
    assert "tool_unavailable:refund" in result["issues"]
    assert [event["event_type"] for event in trace.events] == ["handoff"]


def test_refund_above_payment_is_flagged_without_an_additional_refund_limit() -> None:
    gateway = FakeGateway({
        "get_payments": evidence("payment", PAYMENT_REF, {
            "order_id": "order-5", "payments": [
                {"payment_reference": "pay-a", "amount_brl": "50.00"},
            ],
        }),
        "get_refunds": evidence("refund", REFUND_REF, {
            "order_id": "order-5", "refunds": [
                {"amount_brl": "45.00", "status": "completed"},
                {"amount_brl": "20.00", "status": "pending"},
            ],
        }),
    })
    result = asyncio.run(analyze_payment(CASE, gateway, FakeTrace()))

    assert "refund_exceeds_recorded_payment" in result["issues"]
    assert "maximum_additional_refund_brl" not in result["financials"]
    assert result["financials"]["recommended_refund_brl"] is None


def test_item_totals_detect_payment_mismatch_with_two_evidence_sources() -> None:
    gateway = FakeGateway({
        "get_order_items": evidence("item", ITEM_REF, {
            "order_id": "order-5", "items": [
                {"order_id": "order-5", "price": "70.00", "freight_value": "10.00"},
            ],
        }),
        "get_payments": evidence("payment", PAYMENT_REF, {
            "order_id": "order-5", "payments": [
                {"payment_reference": "pay-a", "payment_value": "100.00"},
            ],
        }),
    })
    trace = FakeTrace()
    result = asyncio.run(analyze_payment(CASE, gateway, trace))

    assert result["financials"]["item_total_brl"] == 70.0
    assert result["financials"]["freight_total_brl"] == 10.0
    assert result["financials"]["verified_order_total_brl"] == 80.0
    assert any(f["topic"] == "payment_reconciliation" and f["status"] == "mismatch"
               and f["evidence_refs"] == [PAYMENT_REF, ITEM_REF]
               for f in result["findings"])
    assert result["evidence_refs"] == [ITEM_REF, PAYMENT_REF]
    assert gateway.calls[0] == ("get_order_items", CASE["case_id"], "order-5")


def test_unsettled_payment_has_no_refundable_limit() -> None:
    gateway = FakeGateway({
        "get_payments": evidence("payment", PAYMENT_REF, {
            "order_id": "order-5", "payments": [
                {"payment_reference": "pay-a", "amount_brl": "50.00", "status": "pending"},
            ],
        }),
        "get_refunds": evidence("refund", REFUND_REF, {
            "order_id": "order-5", "refunds": [],
        }),
    })
    result = asyncio.run(analyze_payment(CASE, gateway, FakeTrace()))

    assert "payment_status_not_settled" in result["issues"]
    assert "maximum_additional_refund_brl" not in result["financials"]
    assert result["financials"]["recommended_refund_brl"] is None


def test_real_mcp_shapes_use_item_rows_and_refund_timeline_events() -> None:
    gateway = FakeGateway({
        "get_order_items": evidence("item", ITEM_REF, [
            {"order_id": "order-5", "order_item_id": "item-1",
             "price": "80.00", "freight_value": "9.00"},
        ]),
        "get_payment_timeline": evidence("payment", PAYMENT_REF, {
            "order_id": "order-5", "payments": [
                {"order_id": "order-5", "payment_sequential": "1",
                 "payment_value": "89.00"},
            ],
            "events": [{"order_id": "order-5", "event_type": "captured",
                        "amount_brl": "89.00", "status": "confirmed"}],
        }),
        "get_refund_timeline": evidence("refund", REFUND_REF, {
            "order_id": "order-5", "events": [
                {"order_id": "order-5", "event_type": "refund_requested",
                 "amount_brl": "89.00", "status": "pending"},
            ],
        }),
    })
    result = asyncio.run(analyze_payment(CASE, gateway, FakeTrace()))

    assert result["financials"]["verified_order_total_brl"] == 89.0
    assert result["financials"]["pending_refund_brl"] == 89.0
    assert result["financials"]["maximum_additional_refund_brl"] == 0.0
    assert result["entities"]["payment_references"] == ["1"]
    assert result["issues"] == []
    assert [call[0] for call in gateway.calls] == [
        "get_order_items", "get_payment_timeline", "get_refund_timeline"
    ]


def test_refund_lifecycle_counts_one_refund_once() -> None:
    gateway = FakeGateway({
        "get_order_payments": evidence("payment", PAYMENT_REF, [
            {"order_id": "order-5", "payment_sequential": "1",
             "payment_value": "100.00"},
        ]),
        "get_refund_timeline": evidence("refund", REFUND_REF, {
            "order_id": "order-5", "events": [
                {"refund_reference": "refund-a", "event_at": "2018-01-01T09:00:00-03:00",
                 "amount_brl": "40.00", "status": "pending"},
                {"refund_reference": "refund-a", "event_at": "2018-01-02T09:00:00-03:00",
                 "amount_brl": "40.00", "status": "completed"},
            ],
        }),
    })
    result = asyncio.run(analyze_payment(CASE, gateway, FakeTrace()))

    assert result["financials"]["completed_refund_brl"] == 40.0
    assert result["financials"]["pending_refund_brl"] == 0.0
    assert result["financials"]["maximum_additional_refund_brl"] == 60.0


def test_unchanged_gateway_accepts_snake_case_mcp_result_via_agent_adapter() -> None:
    class Session:
        async def list_tools(self) -> Any:
            return SimpleNamespace(tools=[SimpleNamespace(name="get_order_payments")])

        async def call_tool(self, name: str, *, arguments: dict[str, str]) -> Any:
            assert name == "get_order_payments"
            assert arguments == {"case_id": CASE["case_id"], "order_id": "order-5"}
            return SimpleNamespace(is_error=False, content=[], structured_content=evidence(
                "payment", PAYMENT_REF, [
                    {"order_id": "order-5", "payment_sequential": "1",
                     "payment_value": "50.00"},
                ]
            ))

    class Contracts:
        def validate_evidence(self, value: Any, label: str) -> None:
            assert value["domain"] == "payment"
            assert label == "MCP tool get_order_payments"

    trace = FakeTrace()
    gateway = EvidenceGateway(Session(), Contracts())
    result = asyncio.run(analyze_payment(CASE, gateway, trace))

    assert result["financials"]["recorded_payment_total_brl"] == 50.0
    assert result["evidence_refs"] == [PAYMENT_REF]
    assert "tool_unavailable:refund" in result["issues"]
    assert trace.events[0]["event_type"] == "tool_result_consumed"
