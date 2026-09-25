from __future__ import annotations

from typing import Any

from .evidence_agent import call_evidence, values
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ACTOR = "payment-agent"
TOOLS = {
    "get_order_payments": "payment",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
}


async def analyze_payment(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    order_id = case["customer_request"]["claimed_order_id"]
    issues: list[dict[str, Any]] = []
    evidence: dict[str, dict[str, Any]] = {}
    for tool_name, domain in TOOLS.items():
        result = await call_evidence(
            gateway,
            trace,
            case_id=case_id,
            actor=ACTOR,
            tool_name=tool_name,
            expected_domain=domain,
            arguments={"order_id": order_id},
            issues=issues,
        )
        if result is not None:
            evidence[tool_name] = result

    payment_data = [
        result["data"]
        for tool_name, result in evidence.items()
        if tool_name in {"get_order_payments", "get_payment_timeline"}
    ]
    payment_references = values(
        payment_data,
        {
            "payment_reference",
            "payment_ref",
            "payment_id",
            "transaction_id",
            "payment_sequential",
        },
    )
    findings = (
        [
            {
                "code": "PAYMENT_EVIDENCE_AVAILABLE",
                "entity_ids": payment_references,
                "evidence_refs": [
                    result["evidence_ref"]
                    for tool_name, result in evidence.items()
                    if tool_name in {"get_order_payments", "get_payment_timeline"}
                ],
            }
        ]
        if payment_data
        else []
    )
    if not payment_data:
        issues.append(
            {"code": "PAYMENT_EVIDENCE_MISSING", "detail": "no authoritative payment data"}
        )
    if "get_refund_timeline" not in evidence:
        issues.append(
            {"code": "REFUND_EVIDENCE_MISSING", "detail": "refund status could not be verified"}
        )

    evidence_refs = [result["evidence_ref"] for result in evidence.values()]
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=ACTOR,
        target="coordinator",
        decision_code="PAYMENT_PARTIAL" if issues else "PAYMENT_VERIFIED",
        evidence_refs=evidence_refs,
        attributes={"issue_count": len(issues), "payment_reference_count": len(payment_references)},
    )
    return {
        "case_id": case_id,
        "actor": ACTOR,
        "status": "partial" if issues else "completed",
        "findings": findings,
        "entities": {"payment_references": payment_references},
        "evidence_refs": evidence_refs,
        "issues": issues,
        "evidence": evidence,
    }
