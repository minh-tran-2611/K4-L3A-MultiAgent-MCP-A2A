from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ACTOR = "payment-agent"
CENT = Decimal("0.01")
TOLERANCE = Decimal("0.10")


class _ResultCompat:
    """Adapt current MCP response fields for the unchanged EvidenceGateway."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    @property
    def isError(self) -> bool:
        return bool(getattr(self._raw, "isError", getattr(self._raw, "is_error", False)))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)


class _SessionCompat:
    def __init__(self, session: Any) -> None:
        self._session = session

    async def list_tools(self) -> Any:
        return await self._session.list_tools()

    async def call_tool(self, *args: Any, **kwargs: Any) -> _ResultCompat:
        return _ResultCompat(await self._session.call_tool(*args, **kwargs))


def _compatible_gateway(gateway: EvidenceGateway) -> EvidenceGateway:
    # The shared gateway is intentionally unchanged; adapt only this agent's session.
    if isinstance(gateway, EvidenceGateway):
        return EvidenceGateway(_SessionCompat(gateway._session), gateway._contracts)
    return gateway


def _money(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(CENT):
        return None
    return amount


def _data(evidence: dict[str, Any], singular: str) -> dict[str, Any] | list[Any]:
    data = evidence.get("data")
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return {}
    nested = data.get(singular)
    return nested if isinstance(nested, dict) else data


def _rows(data: dict[str, Any] | list[Any], plural: str) -> list[dict[str, Any]] | None:
    if isinstance(data, list):
        return data if all(isinstance(row, dict) for row in data) else None
    value = data.get(plural)
    if value is None and any(key in data for key in (
        "payment_value", "refund_amount_brl", "amount_brl", "amount"
    )):
        value = [data]
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        return None
    return value


def _pick_tool(names: set[str], candidates: tuple[str, ...]) -> str | None:
    return next((name for name in candidates if name in names), None)


async def analyze_payment(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Analyze payment and refund evidence for one case; return an internal handoff.

    Monetary findings describe observed transactions. The coordinator must apply
    verified order and policy findings before approving any refund.
    """
    gateway = _compatible_gateway(gateway)
    case_id = case["case_id"]
    request = case.get("customer_request")
    order_id = request.get("claimed_order_id") if isinstance(request, dict) else None
    result: dict[str, Any] = {
        "case_id": case_id,
        "findings": [],
        "entities": {"payment_references": []},
        "evidence_refs": [],
        "issues": [],
        "financials": {"currency": "BRL", "recommended_refund_brl": None, "refund_lines": []},
    }
    findings: list[dict[str, Any]] = result["findings"]
    issues: list[str] = result["issues"]
    refs: list[str] = result["evidence_refs"]

    if not isinstance(order_id, str) or not order_id:
        issues.append("missing_claimed_order_id")
        trace.emit(case_id=case_id, event_type="handoff", actor=ACTOR,
                   target="coordinator", decision_code="PAYMENT_EVIDENCE_MISSING")
        return result

    try:
        available = set(await gateway.list_tools())
    except (OSError, RuntimeError, ValueError) as exc:
        issues.append(f"tool_discovery_failed:{type(exc).__name__}")
        available = set()

    async def fetch(candidates: tuple[str, ...], domain: str) -> dict[str, Any] | None:
        tool = _pick_tool(available, candidates)
        if tool is None:
            issues.append(f"tool_unavailable:{domain}")
            return None
        try:
            evidence = await gateway.call(tool, case_id=case_id, order_id=order_id)
        except (OSError, RuntimeError, ValueError) as exc:
            issues.append(f"tool_failed:{domain}:{type(exc).__name__}")
            return None
        if evidence.get("domain") != domain:
            issues.append(f"unexpected_domain:{domain}")
            return None
        data = _data(evidence, domain)
        if isinstance(data, dict) and data.get("order_id") not in (None, order_id):
            issues.append(f"order_id_conflict:{domain}")
            return None
        if isinstance(data, list) and any(
            not isinstance(row, dict) or row.get("order_id") not in (None, order_id)
            for row in data
        ):
            issues.append(f"order_id_conflict:{domain}")
            return None
        ref = evidence.get("evidence_ref")
        if not isinstance(ref, str) or not ref:
            issues.append(f"missing_evidence_ref:{domain}")
            return None
        return evidence

    def consume(evidence: dict[str, Any], tool: str) -> str:
        ref = evidence["evidence_ref"]
        if ref not in refs:
            refs.append(ref)
            trace.emit(case_id=case_id, event_type="tool_result_consumed", actor=ACTOR,
                       tool_name=tool, evidence_refs=[ref])
        return ref

    item_tool = _pick_tool(available, ("get_order_items", "list_order_items", "get_items"))
    item_total: Decimal | None = None
    freight_total: Decimal | None = None
    item_ref: str | None = None
    if item_tool is not None:
        item_evidence = await fetch((item_tool,), "item")
        if item_evidence is not None:
            item_data = _data(item_evidence, "item")
            item_rows = _rows(item_data, "items")
            if not isinstance(item_rows, list) or not all(
                isinstance(row, dict) for row in item_rows
            ):
                issues.append("item_rows_missing_for_reconciliation")
            elif any(row.get("order_id") not in (None, order_id) for row in item_rows):
                issues.append("item_row_order_id_conflict")
            elif len({str(row.get("order_item_id")) for row in item_rows
                      if row.get("order_item_id") is not None}) < sum(
                          row.get("order_item_id") is not None for row in item_rows
                      ):
                issues.append("duplicate_item_id_for_reconciliation")
            else:
                prices = [_money(row.get("price_brl", row.get("price")))
                          for row in item_rows]
                freight = [_money(row.get("freight_brl", row.get("freight_value")))
                           for row in item_rows]
                if any(value is None for value in prices + freight):
                    issues.append("item_amount_missing_or_invalid")
                else:
                    item_total = sum((value for value in prices if value is not None),
                                     Decimal("0.00"))
                    freight_total = sum((value for value in freight if value is not None),
                                        Decimal("0.00"))
                    item_ref = consume(item_evidence, item_tool)
                    result["financials"]["item_total_brl"] = float(item_total)
                    result["financials"]["freight_total_brl"] = float(freight_total)

    payment_candidates = ("get_payment_timeline", "get_order_payments", "get_payments",
                          "list_payments", "get_payment")
    payment_tool = _pick_tool(available, payment_candidates)
    payment = await fetch(payment_candidates, "payment")
    payment_total: Decimal | None = None
    payment_ref: str | None = None
    payment_rows: list[dict[str, Any]] | None = None
    if payment is not None:
        payment_data = _data(payment, "payment")
        payment_rows = _rows(payment_data, "payments")
        payment_meta = payment_data if isinstance(payment_data, dict) else {}
        if payment_rows is None:
            issues.append("payment_rows_missing")
        elif payment_meta.get("currency", "BRL") != "BRL":
            issues.append("payment_currency_conflict")
        elif any(row.get("currency", "BRL") != "BRL" for row in payment_rows):
            issues.append("payment_row_currency_conflict")
        elif any(row.get("order_id") not in (None, order_id) for row in payment_rows):
            issues.append("payment_row_order_id_conflict")
        else:
            amounts = [_money(row.get("amount_brl", row.get("payment_value", row.get("amount"))))
                       for row in payment_rows]
            if any(amount is None for amount in amounts):
                issues.append("payment_amount_missing_or_invalid")
            elif payment_rows:
                payment_total = sum((amount for amount in amounts if amount is not None),
                                    Decimal("0.00"))
                payment_ref = consume(payment, payment_tool or "")
                result["financials"]["recorded_payment_total_brl"] = float(payment_total)
                identifiers: list[str] = []
                payment_lines: list[dict[str, Any]] = []
                for row in payment_rows:
                    identifier = (row.get("payment_reference") or row.get("payment_id")
                                  or row.get("payment_sequential"))
                    if isinstance(identifier, str) and identifier and identifier not in identifiers:
                        identifiers.append(identifier)
                    amount = _money(row.get("amount_brl", row.get("payment_value",
                                                 row.get("amount"))))
                    payment_lines.append({
                        "payment_reference": identifier if isinstance(identifier, str) else None,
                        "payment_sequential": row.get("payment_sequential"),
                        "amount_brl": float(amount) if amount is not None else None,
                        "status": row.get("status") if isinstance(row.get("status"), str) else None,
                        "evidence_refs": [payment_ref],
                    })
                result["entities"]["payment_references"] = identifiers
                result["financials"]["payment_lines"] = payment_lines
                supplied_identifiers = [row.get("payment_reference") or row.get("payment_id")
                                        or row.get("payment_sequential")
                                        for row in payment_rows]
                if len(identifiers) < sum(isinstance(value, str) and bool(value)
                                          for value in supplied_identifiers):
                    issues.append("duplicate_payment_reference")
                findings.append({"topic": "payment_total", "status": "recorded",
                                 "amount_brl": float(payment_total), "currency": "BRL",
                                 "evidence_refs": [payment_ref]})
                if len(payment_rows) > 1:
                    findings.append({"topic": "split_payment", "status": "multiple_rows",
                                     "row_count": len(payment_rows),
                                     "evidence_refs": [payment_ref]})
                if any(row.get("duplicate_of") or row.get("is_duplicate") is True
                       for row in payment_rows):
                    findings.append({"topic": "duplicate_charge", "status": "flagged",
                                     "evidence_refs": [payment_ref]})

                events = payment_meta.get("events")
                if isinstance(events, list) and events:
                    captured = [event for event in events if isinstance(event, dict)
                                and event.get("event_type") == "captured"
                                and event.get("status") == "confirmed"]
                    if any(event.get("order_id") not in (None, order_id)
                           for event in captured):
                        issues.append("payment_event_order_id_conflict")
                    else:
                        event_amounts = [_money(event.get("amount_brl"))
                                         for event in captured]
                        if any(amount is None for amount in event_amounts):
                            issues.append("captured_amount_invalid")
                        elif captured:
                            captured_total = sum(
                                (amount for amount in event_amounts if amount is not None),
                                Decimal("0.00"),
                            )
                            result["financials"]["confirmed_capture_total_brl"] = float(
                                captured_total
                            )
                            if captured_total != payment_total:
                                issues.append("captured_total_conflicts_with_payment_rows")
                            else:
                                findings.append({"topic": "payment_capture",
                                                 "status": "confirmed",
                                                 "amount_brl": float(captured_total),
                                                 "evidence_refs": [payment_ref]})

        reported_total = _money(payment_meta.get("order_total_brl"))
        expected = item_total + freight_total if (
            item_total is not None and freight_total is not None
        ) else reported_total
        if (reported_total is not None and item_total is not None
                and expected != reported_total):
            issues.append("order_total_source_conflict")
            expected = None
        if payment_total is not None and expected is not None and payment_ref is not None:
            difference = payment_total - expected
            reconciliation_refs = [payment_ref] + ([item_ref] if item_ref else [])
            result["financials"]["verified_order_total_brl"] = float(expected)
            result["financials"]["payment_difference_brl"] = float(difference)
            findings.append({"topic": "payment_reconciliation",
                             "status": "matched" if abs(difference) <= TOLERANCE else "mismatch",
                             "difference_brl": float(difference),
                             "evidence_refs": reconciliation_refs})
            if (payment_rows is not None and len(payment_rows) > 1
                    and abs(difference) <= TOLERANCE
                    and "duplicate_payment_reference" not in issues
                    and not any(row.get("duplicate_of") or row.get("is_duplicate") is True
                                for row in payment_rows)):
                findings.append({"topic": "valid_split_payment", "status": "supported",
                                 "evidence_refs": reconciliation_refs})
        elif payment_total is not None:
            issues.append("order_total_not_verified")

    refund_candidates = ("get_refund_timeline", "get_refunds", "list_refunds", "get_refund")
    refund_tool = _pick_tool(available, refund_candidates)
    refund = await fetch(refund_candidates, "refund")
    if refund is not None:
        refund_data = _data(refund, "refund")
        refund_meta = refund_data if isinstance(refund_data, dict) else {}
        refund_rows = _rows(refund_meta, "refunds")
        if refund_rows is None and "events" in refund_meta:
            refund_rows = _rows(refund_meta["events"], "events")
        ambiguous_events = False
        if refund_rows is not None and "events" in refund_meta and len(refund_rows) > 1:
            latest: dict[str, tuple[datetime, dict[str, Any]]] = {}
            for row in refund_rows:
                reference, timestamp = row.get("refund_reference"), row.get("event_at")
                if (not isinstance(reference, str) or not reference
                        or not isinstance(timestamp, str)):
                    ambiguous_events = True
                    break
                try:
                    occurred_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    if reference not in latest or occurred_at > latest[reference][0]:
                        latest[reference] = (occurred_at, row)
                except (TypeError, ValueError):
                    ambiguous_events = True
                    break
            if not ambiguous_events:
                refund_rows = [entry[1] for entry in latest.values()]
        if refund_rows is None:
            issues.append("refund_rows_missing")
        elif refund_meta.get("currency", "BRL") != "BRL":
            issues.append("refund_currency_conflict")
        elif any(row.get("currency", "BRL") != "BRL" for row in refund_rows):
            issues.append("refund_row_currency_conflict")
        elif any(row.get("order_id") not in (None, order_id) for row in refund_rows):
            issues.append("refund_row_order_id_conflict")
        elif ambiguous_events:
            issues.append("refund_events_cannot_be_deduplicated")
            refund_ref = consume(refund, refund_tool or "")
            findings.append({"topic": "refund_timeline", "status": "ambiguous",
                             "event_count": len(refund_rows), "evidence_refs": [refund_ref]})
        else:
            statuses: dict[str, Decimal] = {}
            refund_lines: list[dict[str, Any]] = []
            valid = True
            for row in refund_rows:
                amount = _money(row.get("amount_brl", row.get("refund_amount_brl",
                                                    row.get("amount"))))
                status = row.get("status")
                if amount is None or not isinstance(status, str) or not status:
                    valid = False
                    break
                key = status.lower()
                statuses[key] = statuses.get(key, Decimal("0.00")) + amount
                refund_lines.append({
                    "refund_reference": row.get("refund_reference")
                    if isinstance(row.get("refund_reference"), str) else None,
                    "payment_reference": row.get("payment_reference")
                    if isinstance(row.get("payment_reference"), str) else None,
                    "amount_brl": float(amount),
                    "status": key,
                    "evidence_refs": [refund["evidence_ref"]],
                })
            if not valid:
                issues.append("refund_amount_or_status_missing")
            else:
                refund_ref = consume(refund, refund_tool or "")
                result["financials"]["verified_refund_ledger_lines"] = refund_lines
                result["financials"]["refund_totals_by_status_brl"] = {
                    status: float(total) for status, total in statuses.items()
                }
                if not refund_rows:
                    findings.append({"topic": "refund_ledger", "status": "no_refund_recorded",
                                     "evidence_refs": [refund_ref]})
                for status, total in statuses.items():
                    findings.append({"topic": "refund_status", "status": status,
                                     "amount_brl": float(total),
                                     "evidence_refs": [refund_ref]})
                completed = sum((statuses.get(key, Decimal("0.00"))
                                 for key in ("completed", "succeeded", "refunded")),
                                Decimal("0.00"))
                pending = sum((statuses.get(key, Decimal("0.00"))
                               for key in ("pending", "processing")), Decimal("0.00"))
                failed = sum((statuses.get(key, Decimal("0.00"))
                              for key in ("failed", "rejected")), Decimal("0.00"))
                result["financials"]["completed_refund_brl"] = float(completed)
                result["financials"]["pending_refund_brl"] = float(pending)
                result["financials"]["failed_refund_brl"] = float(failed)
                known_refund_statuses = {
                    "completed", "succeeded", "refunded", "pending", "processing",
                    "failed", "rejected",
                }
                if set(statuses) - known_refund_statuses:
                    issues.append("unknown_refund_status")
                if payment_rows is not None and any(
                    row.get("status") is not None
                    and str(row["status"]).lower() not in {
                        "captured", "completed", "paid", "succeeded", "settled"
                    }
                    for row in payment_rows
                ):
                    issues.append("payment_status_not_settled")
                if payment_total is not None and completed + pending > payment_total:
                    issues.append("refund_exceeds_recorded_payment")
                elif (payment_total is not None and "unknown_refund_status" not in issues
                      and "payment_status_not_settled" not in issues):
                    result["financials"]["maximum_additional_refund_brl"] = float(
                        payment_total - completed - pending
                    )

    trace.emit(case_id=case_id, event_type="handoff", actor=ACTOR,
               target="coordinator", decision_code="PAYMENT_ANALYZED" if findings else
               "PAYMENT_EVIDENCE_MISSING", evidence_refs=refs[:20])
    return result
