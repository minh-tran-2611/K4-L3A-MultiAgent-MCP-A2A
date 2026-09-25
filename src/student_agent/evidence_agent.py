from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


def records(value: Any) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []

    def walk(current: Any) -> None:
        if isinstance(current, Mapping):
            found.append(current)
            for child in current.values():
                walk(child)
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
            for child in current:
                walk(child)

    walk(value)
    return found


def values(value: Any, keys: set[str]) -> list[str]:
    found: list[str] = []
    for record in records(value):
        for key in keys:
            item = record.get(key)
            if isinstance(item, str) and item:
                found.append(item)
            elif isinstance(item, int) and not isinstance(item, bool):
                found.append(str(item))
    return list(dict.fromkeys(found))


async def call_evidence(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    *,
    case_id: str,
    actor: str,
    tool_name: str,
    expected_domain: str,
    arguments: dict[str, str],
    issues: list[dict[str, Any]],
) -> dict[str, Any] | None:
    try:
        result = await gateway.call(tool_name, case_id=case_id, **arguments)
        if result["domain"] != expected_domain:
            raise ValueError(f"unexpected domain for {tool_name}: {result['domain']!r}")
        evidence_ref = result["evidence_ref"]
        if not isinstance(evidence_ref, str) or not evidence_ref:
            raise ValueError(f"missing evidence reference for {tool_name}")
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[evidence_ref],
        )
        for warning in result.get("warnings", []):
            issues.append(
                {
                    "code": "MCP_WARNING",
                    "source": tool_name,
                    "detail": warning,
                    "evidence_refs": [evidence_ref],
                }
            )
        return result
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        issues.append(
            {
                "code": "TOOL_ERROR",
                "source": tool_name,
                "detail": str(exc),
                "evidence_refs": [],
            }
        )
        return None
