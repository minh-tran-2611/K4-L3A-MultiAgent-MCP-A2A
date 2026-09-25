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


def _item_totals(data: Any) -> tuple[Decimal | None, Decimal | None]:
    rows = [
        row
        for row in records(data)
        if _amount(row.get("price")) is not None or _amount(row.get("freight_value")) is not None
    ]
    if not rows:
        return None, None
    item_total = sum((_amount(row.get("price")) or Decimal("0") for row in rows), Decimal("0"))
    freight_total = sum(
        (_amount(row.get("freight_value")) or Decimal("0") for row in rows),
        Decimal("0"),
    )
    return item_total, freight_total


def _number(data: Any, keys: set[str]) -> Decimal | None:
    for row in records(data):
        for key in keys:
            amount = _amount(row.get(key))
            if amount is not None:
                return amount
    return None


def _signals(data: Any) -> set[str]:
    found: set[str] = set()
    for row in records(data):
        for value in row.values():
            if isinstance(value, str):
                found.add(value.lower().strip().replace("-", "_").replace(" ", "_"))
    return found


def _has_signal(data: Any, *codes: str) -> bool:
    return any(code in signal for signal in _signals(data) for code in codes)


def _datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _evidence_refs(*results: dict[str, Any] | None) -> list[str]:
    return list(
        dict.fromkeys(result["evidence_ref"] for result in results if result is not None)
    )


def _confirmed_issue(
    order: dict[str, Any], payment: dict[str, Any], shipment: dict[str, Any]
) -> tuple[str, list[str]]:
    order_result = order["evidence"].get("order")
    item_result = order["evidence"].get("item")
    seller_result = order["evidence"].get("seller")
    payment_result = payment["evidence"].get("get_order_payments")
    payment_timeline = payment["evidence"].get("get_payment_timeline")
    refund_result = payment["evidence"].get("get_refund_timeline")
    shipment_result = shipment["evidence"].get("get_shipment_summary")
    policy_result = shipment["evidence"].get("get_policy")
    status = None
    if order_result and isinstance(order_result["data"], dict):
        status = order_result["data"].get("order_status")
    paid = _paid_amount(payment_result["data"]) if payment_result else None
    item_total, freight_total = _item_totals(item_result["data"] if item_result else None)
    expected = (
        item_total + freight_total
        if item_total is not None and freight_total is not None
        else None
    )
    reconciled = (
        paid is not None
        and expected is not None
        and abs(paid - expected) <= Decimal("0.10")
    )
    policy_refs = _evidence_refs(policy_result)

    if order["entities"]["order_ids"] and status == "canceled" and paid is not None and paid > 0:
        return "canceled_order_paid", _evidence_refs(order_result, payment_result, policy_result)
    if order["entities"]["order_ids"] and status == "unavailable" and paid is not None and paid > 0:
        return "unavailable_order_paid", _evidence_refs(order_result, payment_result, policy_result)

    payment_rows = [
        row
        for row in records(payment_result["data"] if payment_result else None)
        if _amount(row.get("payment_value")) is not None
    ]
    payment_issue_refs = _evidence_refs(
        item_result, payment_result, payment_timeline, policy_result
    )
    payment_data = [
        result["data"]
        for result in (payment_result, payment_timeline)
        if result is not None
    ]
    if _has_signal(payment_data, "duplicate_charge", "duplicate_capture", "duplicated"):
        return "duplicate_charge", payment_issue_refs
    if _has_signal(payment_data, "payment_mismatch", "capture_mismatch") or (
        paid is not None and expected is not None and not reconciled
    ):
        return "payment_mismatch", payment_issue_refs
    if _has_signal(payment_data, "valid_split_payment", "split_payment_reconciled") or (
        len(payment_rows) >= 2 and reconciled
    ):
        return "valid_split_payment", payment_issue_refs

    if shipment_result:
        delivered = values(
            shipment_result["data"], {"order_delivered_customer_date", "delivered_at"}
        )
        estimated = values(
            shipment_result["data"], {"order_estimated_delivery_date", "estimated_delivery_at"}
        )
        delivered_at = _datetime(delivered[0]) if delivered else None
        estimated_at = _datetime(estimated[0]) if estimated else None
        if delivered_at and estimated_at and delivered_at > estimated_at:
            handoff_values = values(
                shipment_result["data"],
                {"order_delivered_carrier_date", "carrier_handoff_at", "shipped_at"},
            )
            limit_values = values(
                [shipment_result["data"], item_result["data"] if item_result else None],
                {"shipping_limit_date", "shipping_limit_at"},
            )
            handoff = _datetime(handoff_values[0]) if handoff_values else None
            limits = [parsed for value in limit_values if (parsed := _datetime(value))]
            late_seller = bool(handoff and limits and any(handoff > limit for limit in limits))
            shipment_refs = _evidence_refs(
                shipment_result, item_result, seller_result, policy_result
            )
            if late_seller or _has_signal(
                shipment_result["data"], "late_delivery_seller", "seller_handoff_after_limit"
            ):
                return "late_delivery_seller", shipment_refs
            return "late_delivery_logistics", shipment_refs

    if refund_result and _has_signal(refund_result["data"], "refund_failed", "failed"):
        return "refund_failed", _evidence_refs(refund_result, payment_result, policy_result)
    if refund_result and _has_signal(refund_result["data"], "refund_pending", "pending"):
        return "refund_pending", _evidence_refs(refund_result, payment_result, policy_result)

    if order_result and item_result and payment_result and shipment_result and policy_result:
        return "unsupported_claim", _evidence_refs(
            order_result, item_result, payment_result, shipment_result, policy_result
        )
    return "insufficient_evidence", policy_refs


def _financial_resolution(
    issue: str,
    order: dict[str, Any],
    payment: dict[str, Any],
) -> tuple[float, list[dict[str, Any]]]:
    item_result = order["evidence"].get("item")
    payment_result = payment["evidence"].get("get_order_payments")
    refund_result = payment["evidence"].get("get_refund_timeline")
    paid = _paid_amount(payment_result["data"]) if payment_result else None
    item_total, freight_total = _item_totals(item_result["data"] if item_result else None)
    expected = (
        item_total + freight_total
        if item_total is not None and freight_total is not None
        else None
    )
    explicit_refund = _number(
        refund_result["data"] if refund_result else None,
        {
            "recommended_refund_brl",
            "refund_amount_brl",
            "refund_amount",
            "refund_value",
            "amount_brl",
            "amount",
            "refundable_amount_brl",
            "refundable_total_brl",
        },
    )
    amount = Decimal("0")
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        amount = paid or Decimal("0")
    elif issue in {"late_delivery_seller", "late_delivery_logistics"}:
        amount = freight_total or Decimal("0")
    elif issue in {"duplicate_charge", "payment_mismatch"} and paid is not None and expected:
        amount = max(Decimal("0"), paid - expected)
    elif issue in {"refund_pending", "refund_failed"}:
        amount = explicit_refund or paid or Decimal("0")

    amount = amount.quantize(Decimal("0.01"))
    if amount == 0:
        return 0.0, []
    reason_codes = {
        "canceled_order_paid": "FULL_REFUND_CANCELED_ORDER",
        "unavailable_order_paid": "FULL_REFUND_UNAVAILABLE_ORDER",
        "late_delivery_seller": "FREIGHT_REFUND_SELLER_DELAY",
        "late_delivery_logistics": "FREIGHT_REFUND_LOGISTICS_DELAY",
        "duplicate_charge": "DUPLICATE_CHARGE_REFUND",
        "payment_mismatch": "PAYMENT_OVERCHARGE_REFUND",
        "refund_pending": "PENDING_REFUND_AMOUNT",
        "refund_failed": "FAILED_REFUND_AMOUNT",
    }
    entity_ids = (
        order["entities"]["order_ids"]
        if issue
        in {
            "canceled_order_paid",
            "unavailable_order_paid",
            "late_delivery_seller",
            "late_delivery_logistics",
        }
        else payment["entities"]["payment_references"]
    )
    return float(amount), [
        {
            "reason_code": reason_codes[issue],
            "amount_brl": float(amount),
            "entity_id": entity_ids[0] if entity_ids else None,
        }
    ]


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
    consumed_refs = list(
        dict.fromkeys(reference for result in results for reference in result["evidence_refs"])
    )
    if not consumed_refs:
        raise RuntimeError(f"{case_id}: no authoritative evidence was available")
    evidence_refs = issue_refs or consumed_refs
    has_missing_evidence = any(result["status"] == "partial" for result in results)
    if issue == "insufficient_evidence":
        case_status = "needs_investigation"
        confidence = 0.2
        actions = ["Investigate authoritative evidence before deciding a refund"]
    elif issue in {"valid_split_payment", "unsupported_claim"}:
        case_status = "no_action"
        confidence = 0.8 if has_missing_evidence else 0.95
        actions = []
    else:
        case_status = "action_required"
        confidence = 0.8 if has_missing_evidence else 0.95
        actions = {
            "canceled_order_paid": ["issue_full_refund"],
            "unavailable_order_paid": ["issue_full_refund"],
            "late_delivery_seller": ["refund_freight", "review_seller_handoff"],
            "late_delivery_logistics": ["refund_freight", "review_carrier_delay"],
            "duplicate_charge": ["refund_duplicate_charge"],
            "payment_mismatch": ["reconcile_payment", "verify_payment_allocation"],
            "refund_pending": ["verify_refund_completion"],
            "refund_failed": ["retry_refund", "verify_refund_completion"],
        }[issue]

    recommended_refund, refund_lines = _financial_resolution(issue, order, payment)

    claims = []
    for claim in case["customer_request"].get("claims", [])[:5]:
        topic = claim["topic"]
        if issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
            claim_confidence = 0.2
            claim_refs: list[str] = []
        elif topic == issue:
            verdict = "supported"
            claim_confidence = confidence
            claim_refs = issue_refs
        elif topic == "requested_full_refund":
            paid_result = payment["evidence"].get("get_order_payments")
            paid = _paid_amount(paid_result["data"]) if paid_result else None
            if recommended_refund and paid and Decimal(str(recommended_refund)) >= paid:
                verdict = "supported"
            elif recommended_refund:
                verdict = "partially_supported"
            else:
                verdict = "unsupported"
            claim_confidence = confidence
            claim_refs = issue_refs
        else:
            verdict = "unsupported"
            claim_confidence = confidence
            claim_refs = issue_refs
        claims.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": claim_confidence,
                "evidence_refs": claim_refs,
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

    cause_codes = {
        "canceled_order_paid": "ORDER_CANCELED_AFTER_PAYMENT",
        "unavailable_order_paid": "ORDER_UNAVAILABLE_AFTER_PAYMENT",
        "late_delivery_seller": "SELLER_HANDOFF_AFTER_LIMIT",
        "late_delivery_logistics": "CARRIER_DELIVERED_AFTER_ESTIMATE",
        "valid_split_payment": "MULTIPLE_PAYMENTS_RECONCILED",
        "payment_mismatch": "PAYMENT_TOTAL_MISMATCH",
        "duplicate_charge": "DUPLICATE_CHARGE_DETECTED",
        "refund_pending": "REFUND_STILL_PENDING",
        "refund_failed": "REFUND_PROCESSING_FAILED",
        "unsupported_claim": "CLAIM_NOT_SUPPORTED_BY_EVIDENCE",
    }
    causes = (
        [{"cause_code": cause_codes[issue], "rank": 1}]
        if issue in cause_codes
        else []
    )
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
            {"logistics_provider_id", "carrier_id", "carrier_code"},
        )
        responsible_parties = [
            {
                "party_type": "logistics_provider",
                "party_id": provider_ids[0] if provider_ids else None,
            }
        ]
    elif issue in {"canceled_order_paid", "unavailable_order_paid"}:
        responsible_parties = [{"party_type": "platform", "party_id": "OLIST_PLATFORM"}]
    elif issue in {"duplicate_charge", "payment_mismatch", "refund_pending", "refund_failed"}:
        responsible_parties = [
            {"party_type": "payment_provider", "party_id": "PAYMENT_PROVIDER"}
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
            "recommended_refund_brl": recommended_refund,
            "refund_lines": refund_lines,
        },
        "resolution_actions": actions,
    }
    verify_output(case, output, results, trace)
    return output
