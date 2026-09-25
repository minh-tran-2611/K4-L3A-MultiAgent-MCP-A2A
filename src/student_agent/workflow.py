from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .evidence_agent import records, values
from .mcp_gateway import EvidenceGateway
from .order_agent import analyze_order
from .payment_agent import analyze_payment
from .shipment_policy_agent import analyze_shipment_policy
from .trace import TraceWriter
from .verifier import verify_output


def _amount(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def _paid_amount(data: Any) -> Decimal | None:
    payment_rows = [
        row
        for row in records(data)
        if "payment_value" in row and _amount(row["payment_value"]) is not None
    ]
    if payment_rows:
        return sum(
            (_amount(row["payment_value"]) or Decimal("0") for row in payment_rows),
            Decimal("0"),
        )
    return None


def _datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _confirmed_issue(
    order: dict[str, Any], payment: dict[str, Any], shipment: dict[str, Any]
) -> tuple[str, list[str]]:
    order_result = order["evidence"].get("order")
    payment_result = payment["evidence"].get("get_order_payments")
    refund_result = payment["evidence"].get("get_refund_timeline")
    shipment_result = shipment["evidence"].get("get_shipment_summary")
    status = None
    if order_result and isinstance(order_result["data"], dict):
        status = order_result["data"].get("order_status")
    paid = _paid_amount(payment_result["data"]) if payment_result else None
    order_refs = [order_result["evidence_ref"]] if order_result else []
    payment_refs = [payment_result["evidence_ref"]] if payment_result else []

    if order["entities"]["order_ids"] and status == "canceled" and paid is not None and paid > 0:
        return "canceled_order_paid", [*order_refs, *payment_refs]
    if order["entities"]["order_ids"] and status == "unavailable" and paid is not None and paid > 0:
        return "unavailable_order_paid", [*order_refs, *payment_refs]

    if refund_result:
        refund_states = {
            state.lower()
            for state in values(refund_result["data"], {"refund_status", "status", "event_type"})
        }
        if refund_states & {"failed", "refund_failed"}:
            return "refund_failed", [refund_result["evidence_ref"]]
        if refund_states & {"pending", "refund_pending"}:
            return "refund_pending", [refund_result["evidence_ref"]]

    payment_timeline = payment["evidence"].get("get_payment_timeline")
    if payment_timeline:
        payment_events = {
            event.lower()
            for event in values(
                payment_timeline["data"], {"event_type", "issue_code", "anomaly_code"}
            )
        }
        for issue_code in ("duplicate_charge", "payment_mismatch", "valid_split_payment"):
            if issue_code in payment_events:
                return issue_code, [payment_timeline["evidence_ref"]]

    if shipment_result:
        delivered = values(
            shipment_result["data"], {"order_delivered_customer_date", "delivered_at"}
        )
        estimated = values(
            shipment_result["data"], {"order_estimated_delivery_date", "estimated_delivery_at"}
        )
        delivered_at = _datetime(delivered[0]) if delivered else None
        estimated_at = _datetime(estimated[0]) if estimated else None
        responsibility = {
            party.lower()
            for party in values(
                shipment_result["data"], {"responsible_party", "responsibility", "delay_owner"}
            )
        }
        if delivered_at and estimated_at and delivered_at > estimated_at:
            if responsibility & {"seller", "merchant"}:
                return "late_delivery_seller", [shipment_result["evidence_ref"]]
            if responsibility & {"logistics_provider", "logistics", "carrier"}:
                return "late_delivery_logistics", [shipment_result["evidence_ref"]]

    return "insufficient_evidence", []


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    specialists = (
        ("order-agent", analyze_order),
        ("payment-agent", analyze_payment),
        ("shipment-policy-agent", analyze_shipment_policy),
    )
    results: list[dict[str, Any]] = []
    for actor, specialist in specialists:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            decision_code="VERIFY_CLAIMS",
        )
        results.append(await specialist(case, gateway, trace))
    order, payment, shipment = results
    issue, issue_refs = _confirmed_issue(order, payment, shipment)
    issue_refs = list(dict.fromkeys(issue_refs))
    evidence_refs = list(
        dict.fromkeys(reference for result in results for reference in result["evidence_refs"])
    )
    if not evidence_refs:
        raise RuntimeError(f"{case_id}: no authoritative evidence was available")
    has_missing_evidence = any(result["status"] == "partial" for result in results)
    if issue == "insufficient_evidence":
        case_status = "needs_investigation"
        confidence = 0.2
        actions = ["Investigate authoritative evidence before deciding a refund"]
    elif issue in {"valid_split_payment", "unsupported_claim"}:
        case_status = "no_action"
        confidence = 0.75 if has_missing_evidence else 0.9
        actions = []
    else:
        case_status = "action_required"
        confidence = 0.75 if has_missing_evidence else 0.9
        actions = ["Review verified issue and refund eligibility"]

    claims = []
    for claim in case["customer_request"].get("claims", [])[:5]:
        supported = claim["topic"] == issue and bool(issue_refs)
        claims.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": "supported" if supported else "insufficient_evidence",
                "confidence": confidence if supported else 0.2,
                "evidence_refs": issue_refs if supported else [],
            }
        )

    conflicts = []
    for result in results:
        for item in result["issues"]:
            if "MISMATCH" not in item["code"] or len(conflicts) == 5:
                continue
            sources = item.get("observed_ids")
            source_names = list(sources) if isinstance(sources, dict) else [result["actor"], "MCP"]
            conflicts.append(
                {
                    "field": item["code"].lower(),
                    "sources": source_names[:5],
                    "selected_source": None,
                    "resolution_code": "UNRESOLVED",
                }
            )

    causes = [] if issue == "insufficient_evidence" else [{"cause_code": issue.upper(), "rank": 1}]
    responsible_parties = []
    if issue == "late_delivery_seller":
        seller_ids = order["entities"]["seller_ids"]
        responsible_parties = [
            {"party_type": "seller", "party_id": seller_ids[0] if len(seller_ids) == 1 else None}
        ]
    elif issue == "late_delivery_logistics":
        shipment_result = shipment["evidence"].get("get_shipment_summary")
        provider_ids = values(
            shipment_result["data"] if shipment_result else None,
            {"logistics_provider_id", "carrier_id"},
        )
        responsible_parties = [
            {
                "party_type": "logistics_provider",
                "party_id": provider_ids[0] if provider_ids else None,
            }
        ]
    output = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": order["entities"]["order_ids"][:20],
            "item_ids": order["entities"]["item_ids"][:20],
            "seller_ids": order["entities"]["seller_ids"][:20],
            "payment_references": payment["entities"]["payment_references"][:20],
            "shipment_ids": shipment["entities"]["shipment_ids"][:20],
        },
        "claim_assessments": claims,
        "root_cause_analysis": {
            "ranked_causes": causes,
            "responsible_parties": responsible_parties,
        },
        "evidence_refs": evidence_refs[:30],
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": actions,
    }
    verify_output(case, output, results, trace)
    return output
