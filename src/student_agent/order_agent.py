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

    data = [result["data"] for result in evidence.values()]
    order_ids = _ids(data, {"order_id", "order_ids"})
    item_ids = _ids(data, {"item_id", "item_ids", "order_item_id", "order_item_ids"})
    seller_ids = _ids(data, {"seller_id", "seller_ids"})
    item_seller_ids = set(
        _ids(evidence.get("item", {}).get("data"), {"seller_id", "seller_ids"})
    )
    seller_record_ids = set(
        _ids(evidence.get("seller", {}).get("data"), {"seller_id", "seller_ids"})
    )

    findings: list[dict[str, Any]] = []
    order_result = evidence.get("order")
    if order_result:
        order_data = order_result["data"]
        order_status = order_data.get("order_status") if isinstance(order_data, Mapping) else None
        if isinstance(order_status, str) and order_status:
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
        else:
            issues.append(
                {
                    "code": "ORDER_STATUS_NOT_FOUND",
                    "source": "get_order",
                    "detail": "authoritative order evidence has no order_status",
                    "evidence_refs": [order_result["evidence_ref"]],
                }
            )

    item_result = evidence.get("item")
    if item_result and item_ids:
        findings.append(
            {
                "code": "ORDER_ITEMS_CONFIRMED",
                "details": {"item_id_count": len(item_ids)},
                "entity_ids": item_ids,
                "evidence_refs": [item_result["evidence_ref"]],
            }
        )

    seller_result = evidence.get("seller")
    seller_record_ids_list = _ids(
        seller_result["data"] if seller_result else None,
        {"seller_id", "seller_ids"},
    )
    if seller_result and seller_record_ids_list:
        findings.append(
            {
                "code": "ORDER_SELLERS_CONFIRMED",
                "details": {"seller_id_count": len(seller_record_ids_list)},
                "entity_ids": seller_record_ids_list,
                "evidence_refs": [seller_result["evidence_ref"]],
            }
        )

    unexpected_order_ids = set(order_ids) - {claimed_order_id}
    if unexpected_order_ids:
        issues.append(
            {
                "code": "ORDER_ID_MISMATCH",
                "detail": "claimed order ID differs from authoritative evidence",
                "evidence_refs": [
                    result["evidence_ref"]
                    for result in evidence.values()
                    if unexpected_order_ids
                    & set(_ids(result["data"], {"order_id", "order_ids"}))
                ],
            }
        )
    if claimed_order_id not in order_ids:
        issues.append(
            {
                "code": "ORDER_ID_NOT_CONFIRMED",
                "detail": "no authoritative evidence confirmed the claimed order ID",
                "evidence_refs": [],
            }
        )
    if not item_ids:
        issues.append(
            {
                "code": "ITEM_IDS_NOT_FOUND",
                "detail": "no item IDs were found in authoritative evidence",
                "evidence_refs": [item_result["evidence_ref"]] if item_result else [],
            }
        )
    if not seller_ids:
        issues.append(
            {
                "code": "SELLER_IDS_NOT_FOUND",
                "detail": "no seller IDs were found in authoritative evidence",
                "evidence_refs": [seller_result["evidence_ref"]] if seller_result else [],
            }
        )
    if item_seller_ids and seller_record_ids and item_seller_ids != seller_record_ids:
        issues.append(
            {
                "code": "SELLER_ID_MISMATCH",
                "detail": "item seller IDs differ from authoritative seller records",
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
            "order_count": len(order_ids),
            "item_count": len(item_ids),
            "seller_count": len(seller_ids),
            "issue_count": len(issues),
        },
    )
    return {
        "case_id": case_id,
        "actor": ACTOR,
        "status": status,
        "findings": findings,
        "entities": {
            "order_ids": order_ids,
            "item_ids": item_ids,
            "seller_ids": seller_ids,
        },
        "evidence_refs": evidence_refs,
        "issues": issues,
        "evidence": evidence,
    }
