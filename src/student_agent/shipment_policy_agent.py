from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


ACTOR = "shipment-policy-agent"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_value(data: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return None


def _ids(data: Mapping[str, Any], *names: str) -> list[str]:
    values: list[str] = []
    for name in names:
        value = data.get(name)
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate and candidate not in values:
                values.append(candidate)
    return values


def _responsibility(data: Mapping[str, Any]) -> tuple[str | None, str | None]:
    raw = _first_value(data, "responsible_party", "responsible_type", "fault_party")
    if isinstance(raw, Mapping):
        party_type = _first_value(raw, "party_type", "type", "name")
        party_id = _first_value(raw, "party_id", "id", "provider_id")
        return (
            party_type if isinstance(party_type, str) else None,
            party_id if isinstance(party_id, str) else None,
        )
    if isinstance(raw, str):
        return raw, None
    return None, None


async def analyze_shipment_policy(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Verify shipment facts and apply policy without treating the claim as fact."""
    case_id = case["case_id"]
    request = _as_mapping(case.get("customer_request"))
    order_id = request.get("claimed_order_id")
    policy_version = case.get("policy_version")
    findings: list[dict[str, Any]] = []
    entities: dict[str, list[str]] = {"order_ids": [], "shipment_ids": [], "seller_ids": []}
    evidence_refs: list[str] = []
    issues: list[str] = []
    conflicts: list[dict[str, Any]] = []
    shipment_data: Mapping[str, Any] = {}
    policy_data: Mapping[str, Any] = {}

    if not isinstance(order_id, str) or not order_id:
        issues.append("missing claimed_order_id; shipment lookup was skipped")
    else:
        entities["order_ids"].append(order_id)
        try:
            shipment = await gateway.call("get_shipment", case_id=case_id, order_id=order_id)
            shipment_data = _as_mapping(shipment.get("data"))
            evidence_ref = shipment.get("evidence_ref")
            if isinstance(evidence_ref, str):
                evidence_refs.append(evidence_ref)
                trace.emit(case_id=case_id, event_type="tool_result_consumed", actor=ACTOR, tool_name="get_shipment", evidence_refs=[evidence_ref])
            entities["shipment_ids"] = _ids(shipment_data, "shipment_id", "shipment_ids", "tracking_id")
            entities["seller_ids"] = _ids(shipment_data, "seller_id", "seller_ids")
            findings.append({"topic": "shipment", "data": dict(shipment_data), "evidence_refs": [evidence_ref] if isinstance(evidence_ref, str) else []})
        except (RuntimeError, ValueError, TypeError) as exc:
            issues.append(f"shipment evidence unavailable: {exc}")

    if not isinstance(policy_version, str) or not policy_version:
        issues.append("missing policy_version; policy lookup was skipped")
    else:
        try:
            policy = await gateway.call("get_policy", case_id=case_id, policy_version=policy_version)
            policy_data = _as_mapping(policy.get("data"))
            evidence_ref = policy.get("evidence_ref")
            if isinstance(evidence_ref, str):
                evidence_refs.append(evidence_ref)
                trace.emit(case_id=case_id, event_type="tool_result_consumed", actor=ACTOR, tool_name="get_policy", evidence_refs=[evidence_ref])
            findings.append({"topic": "policy", "data": dict(policy_data), "evidence_refs": [evidence_ref] if isinstance(evidence_ref, str) else []})
        except (RuntimeError, ValueError, TypeError) as exc:
            issues.append(f"policy evidence unavailable: {exc}")

    delayed = _first_value(shipment_data, "delayed", "is_delayed", "late")
    cause = _first_value(shipment_data, "delay_cause", "cause_code", "root_cause")
    party_type, party_id = _responsibility(shipment_data)
    policy_action = _first_value(policy_data, "recommended_action", "resolution_action", "action")

    if isinstance(delayed, bool) and delayed:
        conclusion = "shipment delay is supported by shipment evidence"
        if isinstance(cause, str) and cause:
            conclusion += f"; recorded cause: {cause}"
        findings.append({"topic": "late_delivery", "conclusion": conclusion, "evidence_refs": evidence_refs.copy()})
        if party_type is None:
            issues.append("delay is supported but responsible party is not identified")
    elif shipment_data:
        findings.append({"topic": "late_delivery", "conclusion": "shipment evidence does not confirm a delay", "evidence_refs": evidence_refs.copy()})
    else:
        issues.append("insufficient shipment evidence to assess delay or responsibility")

    promised = _first_value(shipment_data, "promised_at", "estimated_delivery_at")
    delivered = _first_value(shipment_data, "delivered_at", "actual_delivery_at")
    if promised and delivered and promised > delivered:
        conflicts.append({"field": "delivery_timeline", "sources": ["shipment.promised_at", "shipment.delivered_at"], "selected_source": None, "resolution_code": "invalid_or_incomplete_timeline"})
        issues.append("shipment timeline is contradictory")

    if policy_data:
        trace.emit(case_id=case_id, event_type="policy_decided", actor=ACTOR, decision_code="policy_evidence_available", evidence_refs=evidence_refs.copy())
    if party_type:
        findings.append({"topic": "responsibility", "party_type": party_type, "party_id": party_id, "evidence_refs": evidence_refs.copy()})
    if policy_action:
        findings.append({"topic": "policy_action", "action": policy_action, "evidence_refs": evidence_refs.copy()})

    trace.emit(case_id=case_id, event_type="handoff", actor=ACTOR, target="coordinator", evidence_refs=evidence_refs.copy(), attributes={"issue_count": len(issues)})
    return {"case_id": case_id, "findings": findings, "entities": entities, "evidence_refs": evidence_refs, "issues": issues, "data_conflicts": conflicts}