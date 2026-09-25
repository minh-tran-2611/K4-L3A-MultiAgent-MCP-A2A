from __future__ import annotations

from typing import Any

from .evidence_agent import call_evidence, values
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ACTOR = "shipment-policy-agent"


async def analyze_shipment_policy(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    order_id = case["customer_request"]["claimed_order_id"]
    issues: list[dict[str, Any]] = []
    evidence: dict[str, dict[str, Any]] = {}
    for tool_name, domain, arguments in (
        ("get_shipment_summary", "shipment", {"order_id": order_id}),
        ("get_policy", "policy", {"policy_version": case["policy_version"]}),
    ):
        result = await call_evidence(
            gateway,
            trace,
            case_id=case_id,
            actor=ACTOR,
            tool_name=tool_name,
            expected_domain=domain,
            arguments=arguments,
            issues=issues,
        )
        if result is not None:
            evidence[tool_name] = result

    shipment = evidence.get("get_shipment_summary")
    shipment_ids = values(shipment["data"] if shipment else None, {"shipment_id", "delivery_id"})
    findings = (
        [
            {
                "code": "SHIPMENT_EVIDENCE_AVAILABLE",
                "entity_ids": shipment_ids,
                "evidence_refs": [shipment["evidence_ref"]],
            }
        ]
        if shipment
        else []
    )
    if shipment is None:
        issues.append(
            {"code": "SHIPMENT_EVIDENCE_MISSING", "detail": "delivery status could not be verified"}
        )
    if "get_policy" not in evidence:
        issues.append({"code": "POLICY_EVIDENCE_MISSING", "detail": "policy could not be verified"})

    evidence_refs = [result["evidence_ref"] for result in evidence.values()]
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=ACTOR,
        target="coordinator",
        decision_code="SHIPMENT_POLICY_PARTIAL" if issues else "SHIPMENT_POLICY_VERIFIED",
        evidence_refs=evidence_refs,
        attributes={"issue_count": len(issues), "shipment_count": len(shipment_ids)},
    )
    return {
        "case_id": case_id,
        "actor": ACTOR,
        "status": "partial" if issues else "completed",
        "findings": findings,
        "entities": {"shipment_ids": shipment_ids},
        "evidence_refs": evidence_refs,
        "issues": issues,
        "evidence": evidence,
    }
