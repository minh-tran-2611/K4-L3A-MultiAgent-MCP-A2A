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


async def investigate_order(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Verify the claimed order and its item/seller relationships through MCP."""
    case_id, claimed_order_id = _case_identity(case)
    evidence: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    warnings: list[str] = []

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
            warnings.extend(f"{tool_name}:{warning}" for warning in result.get("warnings", []))
        except (KeyError, RuntimeError, ValueError) as exc:
            errors.append({"tool": tool_name, "error": str(exc)})

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

    if order_ids and claimed_order_id not in order_ids:
        warnings.append("ORDER_ID_MISMATCH")
    if not order_ids:
        warnings.append("ORDER_ID_NOT_CONFIRMED")
    if not item_ids:
        warnings.append("ITEM_IDS_NOT_FOUND")
    if not seller_ids:
        warnings.append("SELLER_IDS_NOT_FOUND")
    if item_seller_ids and seller_record_ids and item_seller_ids != seller_record_ids:
        warnings.append("SELLER_ID_MISMATCH")

    evidence_refs = [result["evidence_ref"] for result in evidence.values()]
    status = "partial" if errors else "completed"
    verification_status = "partial" if errors else "conflict" if warnings else "verified"
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=ACTOR,
        target="coordinator",
        decision_code=f"ORDER_{verification_status.upper()}",
        evidence_refs=evidence_refs,
        attributes={
            "order_count": len(order_ids),
            "item_count": len(item_ids),
            "seller_count": len(seller_ids),
            "error_count": len(errors),
        },
    )
    return {
        "actor": ACTOR,
        "status": status,
        "verification_status": verification_status,
        "claimed_order_id": claimed_order_id,
        "order_ids": order_ids,
        "item_ids": item_ids,
        "seller_ids": seller_ids,
        "evidence_refs": evidence_refs,
        "evidence": evidence,
        "warnings": list(dict.fromkeys(warnings)),
        "errors": errors,
    }
