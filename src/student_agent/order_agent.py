from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ACTOR = "order-agent"
TOOLS = {
    "get_order": "order",
    "get_order_items": "item",
    "get_sellers": "seller",
}


def _ids(value: Any, keys: set[str]) -> list[str]:
    found: list[str] = []

    def add(candidate: Any) -> None:
        if isinstance(candidate, str) and candidate:
            found.append(candidate)
        elif isinstance(candidate, int) and not isinstance(candidate, bool):
            found.append(str(candidate))
        elif isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
            for item in candidate:
                add(item)

    def walk(current: Any) -> None:
        if isinstance(current, Mapping):
            for key, child in current.items():
                if key in keys:
                    add(child)
                walk(child)
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
            for child in current:
                walk(child)

    walk(value)
    return list(dict.fromkeys(found))


def _case_identity(case: dict[str, Any]) -> tuple[str, str]:
    case_id = case.get("case_id")
    request = case.get("customer_request")
    order_id = request.get("claimed_order_id") if isinstance(request, dict) else None
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case_id must be a non-empty string")
    if not isinstance(order_id, str) or not order_id:
        raise ValueError("customer_request.claimed_order_id must be a non-empty string")
    return case_id, order_id


async def analyze_order(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Verify the claimed order and its item/seller relationships through MCP."""
    case_id, claimed_order_id = _case_identity(case)
    evidence: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, Any]] = []

    for tool_name, expected_domain in TOOLS.items():
        try:
            result = await gateway.call(
                tool_name,
                case_id=case_id,
                order_id=claimed_order_id,
            )
            if result["domain"] != expected_domain:
                raise ValueError(
                    f"{tool_name} returned domain {result['domain']!r}, "
                    f"expected {expected_domain!r}"
                )
            evidence[expected_domain] = result
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=ACTOR,
                tool_name=tool_name,
                evidence_refs=[result["evidence_ref"]],
            )
            issues.extend(
                {
                    "code": "MCP_WARNING",
                    "source": tool_name,
                    "detail": warning,
                    "evidence_refs": [result["evidence_ref"]],
                }
                for warning in result.get("warnings", [])
            )
        except RuntimeError as exc:
            issues.append(
                {
                    "code": "TOOL_ERROR",
                    "source": tool_name,
                    "detail": str(exc),
                    "evidence_refs": [],
                }
            )
        except (KeyError, ValueError) as exc:
            issues.append(
                {
                    "code": "INVALID_TOOL_RESULT",
                    "source": tool_name,
                    "detail": str(exc),
                    "evidence_refs": [],
                }
            )

    order_result = evidence.get("order")
    item_result = evidence.get("item")
    seller_result = evidence.get("seller")
    order_data = order_result["data"] if order_result else None
    item_data = item_result["data"] if item_result else None
    seller_data = seller_result["data"] if seller_result else None

    order_ids_by_domain = {
        domain: _ids(result["data"], {"order_id", "order_ids"})
        for domain, result in evidence.items()
    }
    observed_order_ids = list(
        dict.fromkeys(order_id for ids in order_ids_by_domain.values() for order_id in ids)
    )
    unexpected_order_ids = set(observed_order_ids) - {claimed_order_id}
    confirmed_order_ids = (
        [claimed_order_id]
        if claimed_order_id in observed_order_ids and not unexpected_order_ids
        else []
    )

    observed_item_ids = _ids(
        item_data,
        {"item_id", "item_ids", "order_item_id", "order_item_ids"},
    )
    item_order_ids = set(order_ids_by_domain.get("item", []))
    item_scope_valid = item_order_ids == {claimed_order_id}
    confirmed_item_ids = observed_item_ids if item_scope_valid else []

    item_seller_ids = _ids(item_data, {"seller_id", "seller_ids"})
    seller_record_ids = _ids(seller_data, {"seller_id", "seller_ids"})
    seller_conflict = bool(
        item_seller_ids
        and seller_record_ids
        and set(item_seller_ids) != set(seller_record_ids)
    )
    if seller_conflict:
        confirmed_seller_ids = [
            seller_id for seller_id in item_seller_ids if seller_id in set(seller_record_ids)
        ]
    else:
        confirmed_seller_ids = list(dict.fromkeys([*item_seller_ids, *seller_record_ids]))

    findings: list[dict[str, Any]] = []
    if order_result:
        order_status = order_data.get("order_status") if isinstance(order_data, Mapping) else None
        order_scope_valid = set(order_ids_by_domain.get("order", [])) == {claimed_order_id}
        if not isinstance(order_status, str) or not order_status:
            issues.append(
                {
                    "code": "ORDER_STATUS_NOT_FOUND",
                    "source": "get_order",
                    "detail": "authoritative order evidence has no order_status",
                    "evidence_refs": [order_result["evidence_ref"]],
                }
            )
        elif order_scope_valid:
            status_code = {
                "canceled": "ORDER_CANCELED",
                "unavailable": "ORDER_UNAVAILABLE",
            }.get(order_status, "ORDER_STATUS_CONFIRMED")
            findings.append(
                {
                    "code": status_code,
                    "details": {"order_status": order_status},
                    "entity_ids": _ids(order_data, {"order_id", "order_ids"}),
                    "evidence_refs": [order_result["evidence_ref"]],
                }
            )

    if item_result and confirmed_item_ids:
        findings.append(
            {
                "code": "ORDER_ITEMS_CONFIRMED",
                "details": {"item_id_count": len(confirmed_item_ids)},
                "entity_ids": confirmed_item_ids,
                "evidence_refs": [item_result["evidence_ref"]],
            }
        )

    if confirmed_seller_ids and not seller_conflict:
        seller_evidence_refs = [
            result["evidence_ref"]
            for domain, result in evidence.items()
            if domain in {"item", "seller"}
            and set(_ids(result["data"], {"seller_id", "seller_ids"}))
            & set(confirmed_seller_ids)
        ]
        findings.append(
            {
                "code": "ORDER_SELLERS_CONFIRMED",
                "details": {"seller_id_count": len(confirmed_seller_ids)},
                "entity_ids": confirmed_seller_ids,
                "evidence_refs": seller_evidence_refs,
            }
        )

    if unexpected_order_ids:
        issues.append(
            {
                "code": "ORDER_ID_MISMATCH",
                "detail": "claimed order ID differs from authoritative evidence",
                "observed_ids": order_ids_by_domain,
                "evidence_refs": [
                    result["evidence_ref"]
                    for result in evidence.values()
                    if unexpected_order_ids
                    & set(_ids(result["data"], {"order_id", "order_ids"}))
                ],
            }
        )
    if not confirmed_order_ids:
        issues.append(
            {
                "code": "ORDER_ID_NOT_CONFIRMED",
                "detail": "no authoritative evidence confirmed the claimed order ID",
                "evidence_refs": [
                    evidence[domain]["evidence_ref"]
                    for domain, ids in order_ids_by_domain.items()
                    if ids
                ],
            }
        )
    if not observed_item_ids:
        issues.append(
            {
                "code": "ITEM_IDS_NOT_FOUND",
                "detail": "no item IDs were found in authoritative evidence",
                "evidence_refs": [item_result["evidence_ref"]] if item_result else [],
            }
        )
    elif not item_scope_valid:
        issues.append(
            {
                "code": "ITEM_SCOPE_NOT_CONFIRMED",
                "detail": "item IDs were observed without a matching order ID",
                "evidence_refs": [item_result["evidence_ref"]] if item_result else [],
            }
        )
    if not item_seller_ids and not seller_record_ids:
        issues.append(
            {
                "code": "SELLER_IDS_NOT_FOUND",
                "detail": "no seller IDs were found in authoritative evidence",
                "evidence_refs": [seller_result["evidence_ref"]] if seller_result else [],
            }
        )
    if seller_conflict:
        issues.append(
            {
                "code": "SELLER_ID_MISMATCH",
                "detail": "item seller IDs differ from authoritative seller records",
                "observed_ids": {
                    "get_order_items": item_seller_ids,
                    "get_sellers": seller_record_ids,
                },
                "evidence_refs": [
                    result["evidence_ref"]
                    for domain, result in evidence.items()
                    if domain in {"item", "seller"}
                ],
            }
        )

    evidence_refs = [result["evidence_ref"] for result in evidence.values()]
    status = "partial" if issues else "completed"
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=ACTOR,
        target="coordinator",
        decision_code="ORDER_PARTIAL" if issues else "ORDER_VERIFIED",
        evidence_refs=evidence_refs,
        attributes={
            "order_count": len(confirmed_order_ids),
            "item_count": len(confirmed_item_ids),
            "seller_count": len(confirmed_seller_ids),
            "issue_count": len(issues),
        },
    )
    return {
        "case_id": case_id,
        "actor": ACTOR,
        "status": status,
        "findings": findings,
        "entities": {
            "order_ids": confirmed_order_ids,
            "item_ids": confirmed_item_ids,
            "seller_ids": confirmed_seller_ids,
        },
        "evidence_refs": evidence_refs,
        "issues": issues,
        "evidence": evidence,
    }
