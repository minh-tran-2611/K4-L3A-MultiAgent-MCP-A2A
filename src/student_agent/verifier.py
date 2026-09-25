from __future__ import annotations

from decimal import Decimal
from typing import Any

from .trace import TraceWriter


def verify_output(
    case: dict[str, Any],
    output: dict[str, Any],
    specialist_results: list[dict[str, Any]],
    trace: TraceWriter,
) -> None:
    case_id = case["case_id"]
    if output["case_id"] != case_id:
        raise ValueError("output case_id differs from input")
    if any(result["case_id"] != case_id for result in specialist_results):
        raise ValueError("specialist result belongs to another case")

    consumed_refs = {
        reference for result in specialist_results for reference in result["evidence_refs"]
    }
    output_refs = output["evidence_refs"]
    if len(output_refs) != len(set(output_refs)) or not set(output_refs) <= consumed_refs:
        raise ValueError("output references unconsumed or duplicate evidence")
    for claim in output.get("claim_assessments", []):
        if not set(claim["evidence_refs"]) <= set(output_refs):
            raise ValueError("claim references evidence outside the output")
        if claim["verdict"] == "supported" and not claim["evidence_refs"]:
            raise ValueError("supported claim lacks evidence")

    for entity_ids in output["affected_entities"].values():
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("duplicate entity ID")
    financial = output["financial_resolution"]
    line_total = sum(
        (Decimal(str(line["amount_brl"])) for line in financial["refund_lines"]),
        Decimal("0"),
    )
    if line_total != Decimal(str(financial["recommended_refund_brl"])):
        raise ValueError("refund lines do not add up")
    if financial["recommended_refund_brl"] and not output_refs:
        raise ValueError("refund recommendation lacks evidence")
    if output["assessment"]["case_status"] == "no_action" and output["resolution_actions"]:
        raise ValueError("no_action must not include resolution actions")
    if len(output["resolution_actions"]) != len(set(output["resolution_actions"])):
        raise ValueError("duplicate resolution action")

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code="OUTPUT_VERIFIED",
        evidence_refs=output_refs[:20],
        attributes={
            "specialist_count": len(specialist_results),
            "evidence_count": len(output_refs),
        },
    )
