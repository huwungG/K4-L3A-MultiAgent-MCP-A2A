"""Specialist agents for the L3A investigation workflow.

Each specialist owns a fixed set of MCP tools, scopes the returned rows to the case
window and reports structured findings plus the evidence refs it consumed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

TOOL_TIMEOUT_SECONDS = 60.0
TOOL_ATTEMPTS = 2
# Captures belonging to one checkout happen within hours of order approval.
CHECKOUT_WINDOW = timedelta(hours=24)


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[tuple[str, Any], ...]] = set()
    unique = []
    for row in rows:
        key = tuple(sorted(row.items()))
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


@dataclass
class Evidence:
    tool_name: str
    evidence_ref: str
    domain: str
    data: Any


@dataclass
class Finding:
    actor: str
    facts: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Evidence] = field(default_factory=dict)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def ref(self, tool_name: str) -> str | None:
        item = self.evidence.get(tool_name)
        return item.evidence_ref if item else None


class Specialist:
    actor = "specialist"
    allowed_tools: frozenset[str] = frozenset()

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def fetch(self, finding: Finding, case_id: str, tool_name: str, **arguments: str) -> Any:
        """Call one MCP tool, validate it and emit ``tool_result_consumed``.

        Returns ``None`` when the gateway reports the record does not exist.
        """
        if tool_name not in self.allowed_tools:
            raise PermissionError(f"{self.actor} may not call {tool_name}")
        for attempt in range(1, TOOL_ATTEMPTS + 1):
            try:
                evidence = await asyncio.wait_for(
                    self.gateway.call(tool_name, case_id=case_id, **arguments),
                    timeout=TOOL_TIMEOUT_SECONDS,
                )
                break
            except TimeoutError:
                if attempt == TOOL_ATTEMPTS:
                    finding.missing.append(tool_name)
                    return None
            except RuntimeError:
                # Tool-level error: the gateway has no record in scope for this case.
                finding.missing.append(tool_name)
                return None
        item = Evidence(tool_name, evidence["evidence_ref"], evidence["domain"], evidence["data"])
        finding.evidence[tool_name] = item
        self.trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=self.actor,
            tool_name=tool_name,
            evidence_refs=[item.evidence_ref],
            attributes={"domain": item.domain, "warnings": len(evidence.get("warnings") or [])},
        )
        return item.data


class OrderItemAgent(Specialist):
    actor = "order-item-agent"
    allowed_tools = frozenset({"get_order", "get_order_items", "get_sellers"})

    async def investigate(self, case: dict[str, Any]) -> Finding:
        case_id = case["case_id"]
        order_id = case["customer_request"]["claimed_order_id"]
        finding = Finding(self.actor)
        order = await self.fetch(finding, case_id, "get_order", order_id=order_id)
        if not isinstance(order, dict) or order.get("order_id") != order_id:
            finding.facts["order_found"] = False
            return finding
        items = await self.fetch(finding, case_id, "get_order_items", order_id=order_id) or []
        sellers = await self.fetch(finding, case_id, "get_sellers", order_id=order_id) or []

        purchased = parse_time(order["order_purchase_timestamp"])
        opened = parse_time(case["opened_at"])
        in_scope = [row for row in _dedupe(items) if row.get("order_id") == order_id]

        # One authoritative row per order item: the first shipping limit of this checkout.
        primary: dict[str, dict[str, Any]] = {}
        for row in sorted(in_scope, key=lambda r: parse_time(r["shipping_limit_date"])):
            limit = parse_time(row["shipping_limit_date"])
            if purchased <= limit <= opened and row["order_item_id"] not in primary:
                primary[row["order_item_id"]] = row
        excluded = len(in_scope) - len(primary)
        if excluded:
            finding.conflicts.append(
                {
                    "field": "order_items.shipping_limit_date",
                    "sources": ["get_order_items:checkout_row", "get_order_items:other_row"],
                    "selected_source": "get_order_items:checkout_row",
                    "resolution_code": "KEEP_ROW_MATCHING_ORDER_CHECKOUT",
                }
            )
        rows = list(primary.values())
        seller_ids = sorted({row["seller_id"] for row in rows})
        known_sellers = {row.get("seller_id") for row in sellers}
        finding.facts.update(
            order_found=True,
            order_id=order_id,
            order_status=order["order_status"],
            purchased_at=order["order_purchase_timestamp"],
            approved_at=order["order_approved_at"],
            delivered_carrier_at=order["order_delivered_carrier_date"],
            delivered_customer_at=order["order_delivered_customer_date"],
            estimated_delivery_at=order["order_estimated_delivery_date"],
            item_ids=sorted(primary),
            items=rows,
            seller_ids=seller_ids,
            sellers_verified=set(seller_ids) <= known_sellers,
            order_total=str(sum((money(r["price"]) + money(r["freight_value"]) for r in rows),
                                Decimal("0.00"))),
            freight_total=str(sum((money(r["freight_value"]) for r in rows), Decimal("0.00"))),
        )
        return finding


class PaymentAgent(Specialist):
    actor = "payment-agent"
    allowed_tools = frozenset({"get_payment_timeline", "get_refund_timeline"})

    async def investigate(self, case: dict[str, Any], order_facts: dict[str, Any]) -> Finding:
        case_id = case["case_id"]
        order_id = order_facts["order_id"]
        finding = Finding(self.actor)
        timeline = await self.fetch(finding, case_id, "get_payment_timeline", order_id=order_id)
        refunds = await self.fetch(finding, case_id, "get_refund_timeline", order_id=order_id)

        purchased = parse_time(order_facts["purchased_at"])
        approved = parse_time(order_facts["approved_at"]) or purchased
        opened = parse_time(case["opened_at"])

        def in_window(event: dict[str, Any]) -> bool:
            at = parse_time(event.get("event_at"))
            if event.get("order_id") != order_id or at is None:
                return False
            return purchased <= at <= opened

        def in_checkout(event: dict[str, Any]) -> bool:
            at = parse_time(event["event_at"])
            return approved <= at <= approved + CHECKOUT_WINDOW

        events = _dedupe((timeline or {}).get("events", []))
        scoped = [e for e in events if in_window(e)]
        captures = [
            e for e in scoped
            if e["event_type"] == "captured" and e["status"] == "confirmed" and in_checkout(e)
        ]
        capture_amounts = [money(e["amount_brl"]) for e in captures]
        mismatches = [
            e for e in scoped
            if e["event_type"] == "reconciliation_mismatch" and e["status"] == "open"
            and in_checkout(e) and money(e["amount_brl"]) in capture_amounts
        ]
        refund_events = [
            e for e in _dedupe((refunds or {}).get("events", []))
            if in_window(e) and money(e["amount_brl"]) in capture_amounts
        ]
        ignored = len(events) - len(captures) - len(mismatches)
        ignored += len((refunds or {}).get("events", [])) - len(refund_events)
        if ignored:
            finding.conflicts.append(
                {
                    "field": "payment_lifecycle.events",
                    "sources": ["get_payment_timeline", "get_refund_timeline", "get_order"],
                    "selected_source": "get_order",
                    "resolution_code": "EXCLUDE_EVENTS_OUTSIDE_CASE_CHECKOUT",
                }
            )
        finding.facts.update(
            captures=[str(a) for a in capture_amounts],
            captured_total=str(sum(capture_amounts, Decimal("0.00"))),
            has_mismatch=bool(mismatches),
            mismatch_amount=str(money(mismatches[0]["amount_brl"])) if mismatches else None,
            refund_status=refund_events[-1]["status"] if refund_events else None,
            refund_amount=str(money(refund_events[-1]["amount_brl"])) if refund_events else None,
        )
        return finding


class ShipmentAgent(Specialist):
    actor = "shipment-agent"
    allowed_tools = frozenset({"get_shipment_summary"})

    async def investigate(self, case: dict[str, Any], order_facts: dict[str, Any]) -> Finding:
        case_id = case["case_id"]
        order_id = order_facts["order_id"]
        finding = Finding(self.actor)
        summary = await self.fetch(finding, case_id, "get_shipment_summary", order_id=order_id)
        summary = summary or {}
        delivered = parse_time(order_facts["delivered_customer_at"])
        estimated = parse_time(order_facts["estimated_delivery_at"])
        carrier = parse_time(order_facts["delivered_carrier_at"])
        limits = [parse_time(row["shipping_limit_date"]) for row in order_facts["items"]]

        late = bool(delivered and estimated and delivered > estimated)
        seller_late_handoff = bool(carrier and limits and carrier > min(limits))
        # Shipment summary must agree with the authoritative order row.
        if summary and (
            summary.get("delivered_customer_at") != order_facts["delivered_customer_at"]
            or summary.get("estimated_delivery_at") != order_facts["estimated_delivery_at"]
        ):
            finding.conflicts.append(
                {
                    "field": "shipment.delivery_timestamps",
                    "sources": ["get_shipment_summary", "get_order"],
                    "selected_source": "get_order",
                    "resolution_code": "PREFER_AUTHORITATIVE_ORDER_ROW",
                }
            )
        purchased = parse_time(order_facts["purchased_at"])
        opened = parse_time(case["opened_at"])
        late_events = [
            e for e in summary.get("events", [])
            if e.get("event_type") == "delivered_late"
            and purchased <= parse_time(e["event_at"]) <= opened
        ]
        if late_events and not late:
            finding.conflicts.append(
                {
                    "field": "shipment.delivered_late_event",
                    "sources": ["get_shipment_summary", "get_order"],
                    "selected_source": "get_order",
                    "resolution_code": "EVENT_CONTRADICTED_BY_ORDER_TIMESTAMPS",
                }
            )
        finding.facts.update(
            delivered_late=late,
            seller_late_handoff=seller_late_handoff,
            late_days=(delivered - estimated).days if late else 0,
        )
        return finding


class PolicyAgent(Specialist):
    actor = "policy-agent"
    allowed_tools = frozenset({"get_policy"})

    async def load(self, case: dict[str, Any]) -> Finding:
        finding = Finding(self.actor)
        policy = await self.fetch(
            finding, case["case_id"], "get_policy", policy_version=case["policy_version"]
        )
        valid = isinstance(policy, dict) and policy.get("policy_version") == case["policy_version"]
        finding.facts["rules"] = policy.get("rules", {}) if valid else {}
        finding.facts["currency"] = policy.get("currency") if valid else None
        return finding

    def decide(self, case_id: str, finding: Finding, primary_issue: str) -> dict[str, Any] | None:
        rule = finding.facts["rules"].get(primary_issue)
        self.trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor=self.actor,
            decision_code=rule["recommended_action"] if rule else "NO_POLICY_RULE",
            tool_name="get_policy",
            evidence_refs=[finding.ref("get_policy")] if finding.ref("get_policy") else None,
            attributes={"primary_issue": primary_issue},
        )
        return rule
