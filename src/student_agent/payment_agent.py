from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .evidence_agent import call_evidence, records, values
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ACTOR = "payment-agent"
TOOLS = {
    "get_order_payments": "payment",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
}
CENT = Decimal("0.01")


def _money(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    try:
        return amount.quantize(CENT) if amount == amount.quantize(CENT) else None
    except InvalidOperation:
        return None


def _rows(data: Any, amount_keys: tuple[str, ...]) -> list[dict[str, Any]]:
    return [
        dict(record)
        for record in records(data)
        if any(key in record for key in amount_keys)
    ]


def _latest_refund_rows(data: Any, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [
        dict(record)
        for record in records(data)
        if any(key in record for key in ("amount_brl", "refund_amount_brl"))
        and any(key in record for key in ("status", "event_type", "refund_status"))
    ]
    if len(rows) <= 1:
        return rows

    latest: dict[str, tuple[datetime, dict[str, Any]]] = {}
    for row in rows:
        reference, timestamp = row.get("refund_reference"), row.get("event_at")
        if not isinstance(reference, str) or not reference or not isinstance(timestamp, str):
            issues.append({
                "code": "REFUND_EVENTS_AMBIGUOUS",
                "detail": "multiple refund events cannot be grouped by refund reference",
                "evidence_refs": [],
            })
            return []
        try:
            occurred_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if reference not in latest or occurred_at > latest[reference][0]:
                latest[reference] = (occurred_at, row)
        except (TypeError, ValueError):
            issues.append({
                "code": "REFUND_EVENTS_AMBIGUOUS",
                "detail": "refund event timestamp is invalid",
                "evidence_refs": [],
            })
            return []
    return [item[1] for item in latest.values()]


async def analyze_payment(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Analyze payment/refund evidence and return a case-scoped coordinator handoff."""
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

    refs_by_tool = {
        tool_name: result["evidence_ref"] for tool_name, result in evidence.items()
    }
    payment_data = evidence.get("get_order_payments", {}).get("data")
    timeline = evidence.get("get_payment_timeline", {}).get("data")
    refund_data = evidence.get("get_refund_timeline", {}).get("data")

    payment_rows = _rows(payment_data, ("payment_value", "amount_brl", "amount"))
    payment_total: Decimal | None = None
    payment_lines: list[dict[str, Any]] = []
    findings_multiple: dict[str, Any] | None = None
    payment_references = values(
        payment_data, {"payment_reference", "payment_ref", "payment_id"}
    )
    if payment_rows:
        amounts = [
            _money(row.get("payment_value", row.get("amount_brl", row.get("amount"))))
            for row in payment_rows
        ]
        if any(amount is None for amount in amounts):
            issues.append({
                "code": "PAYMENT_AMOUNT_INVALID",
                "detail": "one or more payment rows has an invalid amount",
                "evidence_refs": [refs_by_tool["get_order_payments"]],
            })
        else:
            payment_total = sum(
                (amount for amount in amounts if amount is not None), Decimal("0.00")
            )
            payment_lines = [
                {
                    "payment_reference": row.get("payment_reference")
                    or row.get("payment_ref") or row.get("payment_id"),
                    "payment_sequential": row.get("payment_sequential"),
                    "amount_brl": float(amount),
                    "payment_type": row.get("payment_type"),
                    "evidence_refs": [refs_by_tool["get_order_payments"]],
                }
                for row, amount in zip(payment_rows, amounts, strict=True)
                if amount is not None
            ]
            if len(payment_rows) > 1:
                findings_multiple = {
                    "code": "SPLIT_PAYMENT_ROWS_OBSERVED",
                    "row_count": len(payment_rows),
                    "evidence_refs": [refs_by_tool["get_order_payments"]],
                }
                payment_references = list(dict.fromkeys(
                    payment_references + [
                        str(row["payment_sequential"])
                        for row in payment_rows
                        if row.get("payment_sequential") is not None
                    ]
                ))
    else:
        if payment_data is not None:
            issues.append({
                "code": "PAYMENT_ROWS_MISSING",
                "detail": "payment response contained no identifiable payment rows",
                "evidence_refs": [refs_by_tool["get_order_payments"]],
            })

    if payment_rows:
        sequential = [str(row["payment_sequential"]) for row in payment_rows
                      if row.get("payment_sequential") is not None]
        duplicate_sequences = sorted({value for value in sequential if sequential.count(value) > 1})
        if duplicate_sequences:
            issues.append({
                "code": "DUPLICATE_PAYMENT_REFERENCE_SUSPECTED",
                "detail": (
                    "payment sequence occurs on multiple rows; "
                    "transaction identity needs review"
                ),
                "observed_ids": {"payment_sequential": duplicate_sequences},
                "evidence_refs": [refs_by_tool["get_order_payments"]],
            })

    capture_rows = [
        dict(record)
        for record in records(timeline)
        if record.get("event_type") == "captured" and record.get("status") == "confirmed"
    ]
    capture_amounts = [_money(row.get("amount_brl")) for row in capture_rows]
    captured_total = None
    if capture_rows and all(amount is not None for amount in capture_amounts):
        captured_total = sum(
            (amount for amount in capture_amounts if amount is not None), Decimal("0.00")
        )
        if payment_total is not None and captured_total != payment_total:
            issues.append({
                "code": "CAPTURE_TOTAL_MISMATCH",
                "detail": "confirmed capture events do not equal recorded payment rows",
                "observed_ids": {
                    "payment_rows_brl": float(payment_total),
                    "confirmed_capture_brl": float(captured_total),
                },
                "evidence_refs": [refs_by_tool["get_order_payments"],
                                  refs_by_tool["get_payment_timeline"]],
            })

    observed_anomalies = {
        anomaly.lower()
        for anomaly in values(timeline, {"issue_code", "anomaly_code"})
        if anomaly.lower() in {"duplicate_charge", "payment_mismatch", "valid_split_payment"}
    }

    refund_rows = _latest_refund_rows(refund_data, issues)
    refund_totals: dict[str, Decimal] = {}
    refund_lines: list[dict[str, Any]] = []
    refund_statuses = {"completed": "completed", "succeeded": "completed",
                       "refunded": "completed", "pending": "pending",
                       "processing": "pending", "failed": "failed", "rejected": "failed"}
    for row in refund_rows:
        raw_status = row.get("refund_status") or row.get("status") or row.get("event_type")
        status = str(raw_status).lower() if raw_status is not None else ""
        if status.startswith("refund_"):
            status = status.removeprefix("refund_")
        amount = _money(row.get("amount_brl", row.get("refund_amount_brl")))
        if amount is None or status not in refund_statuses:
            issues.append({
                "code": "REFUND_AMOUNT_OR_STATUS_INVALID",
                "detail": "refund event amount or status could not be verified",
                "evidence_refs": [refs_by_tool["get_refund_timeline"]],
            })
            continue
        category = refund_statuses[status]
        refund_totals[category] = refund_totals.get(category, Decimal("0.00")) + amount
        refund_lines.append({
            "refund_reference": row.get("refund_reference"),
            "payment_reference": row.get("payment_reference"),
            "amount_brl": float(amount),
            "status": category,
            "evidence_refs": [refs_by_tool["get_refund_timeline"]],
        })

    findings: list[dict[str, Any]] = []
    if payment_data is not None:
        findings.append({
            "code": "PAYMENT_EVIDENCE_AVAILABLE",
            "entity_ids": payment_references,
            "recorded_total_brl": float(payment_total) if payment_total is not None else None,
            "evidence_refs": [refs_by_tool["get_order_payments"]],
        })
    if findings_multiple is not None:
        findings.append(findings_multiple)
    if captured_total is not None:
        findings.append({
            "code": "PAYMENT_CAPTURE_CONFIRMED",
            "amount_brl": float(captured_total),
            "evidence_refs": [refs_by_tool["get_payment_timeline"]],
        })
    for anomaly in sorted(observed_anomalies):
        findings.append({
            "code": "PAYMENT_ANOMALY_OBSERVED",
            "anomaly_code": anomaly,
            "evidence_refs": [refs_by_tool["get_payment_timeline"]],
        })
    if refund_data is not None:
        findings.append({
            "code": "REFUND_STATUS_OBSERVED",
            "totals_by_status_brl": {key: float(value) for key, value in refund_totals.items()},
            "evidence_refs": [refs_by_tool["get_refund_timeline"]],
        })

    financials: dict[str, Any] = {
        "currency": "BRL",
        "recommended_refund_brl": None,
        "refund_lines": [],
        "verified_refund_ledger_lines": refund_lines,
    }
    if payment_total is not None:
        financials["recorded_payment_total_brl"] = float(payment_total)
        financials["payment_lines"] = payment_lines
    if captured_total is not None:
        financials["confirmed_capture_total_brl"] = float(captured_total)
    if refund_data is not None and not any(
        issue["code"] in {"REFUND_EVENTS_AMBIGUOUS", "REFUND_AMOUNT_OR_STATUS_INVALID"}
        for issue in issues
    ):
        completed = refund_totals.get("completed", Decimal("0.00"))
        pending = refund_totals.get("pending", Decimal("0.00"))
        failed = refund_totals.get("failed", Decimal("0.00"))
        financials.update({
            "completed_refund_brl": float(completed),
            "pending_refund_brl": float(pending),
            "failed_refund_brl": float(failed),
        })
        if payment_total is not None and completed + pending <= payment_total:
            financials["maximum_additional_refund_brl"] = float(
                payment_total - completed - pending
            )
        elif payment_total is not None:
            issues.append({
                "code": "REFUND_TOTAL_EXCEEDS_PAYMENT",
                "detail": "completed and pending refunds exceed recorded payments",
                "evidence_refs": [refs_by_tool["get_order_payments"],
                                  refs_by_tool["get_refund_timeline"]],
            })

    if not payment_data:
        issues.append({
            "code": "PAYMENT_EVIDENCE_MISSING",
            "detail": "no authoritative payment data",
            "evidence_refs": [],
        })
    if "get_refund_timeline" not in evidence:
        issues.append({
            "code": "REFUND_EVIDENCE_MISSING",
            "detail": "refund status could not be verified",
            "evidence_refs": [],
        })

    evidence_refs = list(dict.fromkeys(result["evidence_ref"] for result in evidence.values()))
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
        "financials": financials,
    }
