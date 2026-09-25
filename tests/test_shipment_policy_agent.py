from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.shipment_policy_agent import analyze_shipment_policy
from student_agent.trace import TraceWriter


class FakeGateway:
    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        response = self.responses.get(tool_name)
        if response is None:
            raise RuntimeError("not found")
        return response


def make_case() -> dict[str, Any]:
    return {"case_id": "L3A_CASE_002", "customer_request": {"claimed_order_id": "order-1", "claims": []}, "policy_version": "EC_POLICY_V1"}


def make_trace(tmp_path: Path) -> TraceWriter:
    root = Path(__file__).resolve().parents[1]
    return TraceWriter(tmp_path / "trace.jsonl", Contracts(root / "contracts" / "schemas"))


def test_late_delivery_keeps_evidence_and_responsibility(tmp_path: Path) -> None:
    gateway = FakeGateway({
        "get_shipment": {"evidence_ref": "ev_shipment_12345678901234567890", "data": {"shipment_id": "ship-1", "delayed": True, "delay_cause": "LOGISTICS_DELAY", "responsible_party": {"party_type": "logistics_provider", "party_id": "carrier-1"}}},
        "get_policy": {"evidence_ref": "ev_policy_12345678901234567890", "data": {"recommended_action": "open_carrier_investigation"}},
    })

    result = asyncio.run(analyze_shipment_policy(make_case(), gateway, make_trace(tmp_path)))

    assert result["case_id"] == "L3A_CASE_002"
    assert result["entities"]["shipment_ids"] == ["ship-1"]
    assert result["evidence_refs"] == ["ev_shipment_12345678901234567890", "ev_policy_12345678901234567890"]
    assert any(item.get("topic") == "responsibility" for item in result["findings"])
    assert result["issues"] == []
    assert all(call[1] == "L3A_CASE_002" for call in gateway.calls)


def test_missing_shipment_evidence_does_not_assign_blame(tmp_path: Path) -> None:
    result = asyncio.run(analyze_shipment_policy(make_case(), FakeGateway({}), make_trace(tmp_path)))

    assert result["evidence_refs"] == []
    assert result["entities"]["shipment_ids"] == []
    assert any("insufficient shipment evidence" in issue for issue in result["issues"])
    assert not any(item.get("topic") == "responsibility" for item in result["findings"])