from __future__ import annotations

from decimal import Decimal
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .a2a import A2ABus
from .agents import (
    Finding,
    OrderItemAgent,
    PaymentAgent,
    PolicyAgent,
    ShipmentAgent,
    money,
)
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

COORDINATOR = "coordinator"
VERIFIER = "verifier"

# Evidence each conclusion must cite (tool names); anything else is left out of the output.
EVIDENCE_PLAN: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": ("get_order", "get_payment_timeline", "get_policy"),
    "unavailable_order_paid": (
        "get_order", "get_order_items", "get_sellers", "get_payment_timeline", "get_policy",
    ),
    "late_delivery_seller": (
        "get_order", "get_shipment_summary", "get_order_items", "get_sellers", "get_policy",
    ),
    "late_delivery_logistics": (
        "get_order", "get_shipment_summary", "get_order_items", "get_policy",
    ),
    "valid_split_payment": ("get_order", "get_payment_timeline", "get_policy"),
    "payment_mismatch": ("get_order", "get_payment_timeline", "get_policy"),
    "duplicate_charge": ("get_order", "get_payment_timeline", "get_policy"),
    "refund_pending": ("get_order", "get_payment_timeline", "get_refund_timeline", "get_policy"),
    "refund_failed": ("get_order", "get_payment_timeline", "get_refund_timeline", "get_policy"),
    "unsupported_claim": (
        "get_order", "get_shipment_summary", "get_payment_timeline", "get_policy",
    ),
    "insufficient_evidence": ("get_order", "get_policy"),
}

REFUND_TARGET = {
    "late_delivery_seller": "item",
    "late_delivery_logistics": "item",
}


def classify(order: dict[str, Any], payment: dict[str, Any], shipment: dict[str, Any]) -> str:
    """Coordinator decision table, most specific signal first."""
    captured = money(payment.get("captured_total") or "0")
    if order["order_status"] == "canceled" and captured > 0:
        return "canceled_order_paid"
    if order["order_status"] == "unavailable" and captured > 0:
        return "unavailable_order_paid"
    if payment.get("refund_status") == "failed":
        return "refund_failed"
    if payment.get("refund_status") in {"pending", "requested", "processing"}:
        return "refund_pending"
    if payment.get("has_mismatch"):
        return "payment_mismatch"
    if shipment.get("delivered_late"):
        if shipment.get("seller_late_handoff"):
            return "late_delivery_seller"
        return "late_delivery_logistics"
    captures = [money(value) for value in payment.get("captures", [])]
    if len(captures) >= 2:
        if captured == money(order["order_total"]):
            return "valid_split_payment"
        if len(set(captures)) < len(captures):
            return "duplicate_charge"
        return "payment_mismatch"
    return "unsupported_claim"


def _claim_verdict(topic: str, primary_issue: str, refund: Decimal, action: str,
                   case_status: str) -> str:
    if primary_issue == "insufficient_evidence":
        return "insufficient_evidence"
    if topic == "requested_full_refund":
        if refund <= 0 and case_status == "needs_investigation":
            return "insufficient_evidence"
        if refund <= 0:
            return "unsupported"
        return "supported" if action == "issue_refund" else "partially_supported"
    if topic == "unsupported_claim":
        return "unsupported" if primary_issue == "unsupported_claim" else "partially_supported"
    return "supported" if topic == primary_issue else "unsupported"


def _refs(findings: list[Finding], tools: tuple[str, ...]) -> list[str]:
    by_tool = {tool: ev.evidence_ref for f in findings for tool, ev in f.evidence.items()}
    return [by_tool[tool] for tool in tools if tool in by_tool]


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    request = case["customer_request"]
    bus = A2ABus(case_id, trace)
    order_agent = OrderItemAgent(gateway, trace)
    payment_agent = PaymentAgent(gateway, trace)
    shipment_agent = ShipmentAgent(gateway, trace)
    policy_agent = PolicyAgent(gateway, trace)

    # 1. Coordinator scopes the case to the claimed order and the policy version.
    bus.send(COORDINATOR, policy_agent.actor, "assign:load_policy",
             {"policy_version": case["policy_version"]})
    policy = await policy_agent.load(case)
    bus.send(policy_agent.actor, COORDINATOR, "policy_loaded", evidence_refs=_refs([policy], (
        "get_policy",)))

    bus.send(COORDINATOR, order_agent.actor, "assign:verify_order_scope",
             {"order_id": request["claimed_order_id"]})
    order = await order_agent.investigate(case)
    bus.send(order_agent.actor, COORDINATOR,
             "order_scoped" if order.facts.get("order_found") else "order_not_found",
             evidence_refs=[e.evidence_ref for e in order.evidence.values()])

    findings = [policy, order]
    if order.facts.get("order_found"):
        # 2. Payment and shipment specialists work from the verified order facts.
        bus.send(COORDINATOR, payment_agent.actor, "assign:reconcile_payments",
                 {"order_id": order.facts["order_id"]})
        payment = await payment_agent.investigate(case, order.facts)
        bus.send(payment_agent.actor, COORDINATOR, "payments_reconciled",
                 evidence_refs=[e.evidence_ref for e in payment.evidence.values()])

        bus.send(COORDINATOR, shipment_agent.actor, "assign:check_delivery",
                 {"order_id": order.facts["order_id"]})
        shipment = await shipment_agent.investigate(case, order.facts)
        bus.send(shipment_agent.actor, COORDINATOR, "delivery_checked",
                 evidence_refs=[e.evidence_ref for e in shipment.evidence.values()])
        findings += [payment, shipment]
        primary_issue = classify(order.facts, payment.facts, shipment.facts)
    else:
        payment = shipment = Finding("none")
        primary_issue = "insufficient_evidence"

    # 3. Policy agent maps the issue to status, action, refund and responsibility.
    bus.send(COORDINATOR, policy_agent.actor, "assign:apply_policy",
             {"primary_issue": primary_issue})
    rule = policy_agent.decide(case_id, policy, primary_issue)
    bus.send(policy_agent.actor, COORDINATOR, "policy_applied",
             evidence_refs=_refs([policy], ("get_policy",)))

    output = _draft_output(case, primary_issue, rule, order, payment, shipment, findings)

    # 4. Verifier checks invariants before the coordinator finalizes.
    bus.send(COORDINATOR, VERIFIER, "assign:verify_output")
    checks = verify(output, order, payment, findings, rule)
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        output["assessment"]["confidence"] = min(output["assessment"]["confidence"], 0.5)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor=VERIFIER,
        decision_code="PASS" if not failed else "PASS_WITH_WARNINGS",
        evidence_refs=output["evidence_refs"],
        attributes={"checks": len(checks), "failed": ",".join(failed) or None},
    )
    bus.send(VERIFIER, COORDINATOR, "verified" if not failed else "verified_with_warnings",
             evidence_refs=output["evidence_refs"])
    return output


def _draft_output(
    case: dict[str, Any],
    primary_issue: str,
    rule: dict[str, Any] | None,
    order: Finding,
    payment: Finding,
    shipment: Finding,
    findings: list[Finding],
) -> dict[str, Any]:
    request = case["customer_request"]
    facts = order.facts
    order_id = facts.get("order_id", request["claimed_order_id"])
    seller_ids = facts.get("seller_ids", [])
    captured = money(payment.facts.get("captured_total") or "0")

    if rule is None:
        primary_issue = "insufficient_evidence"
        case_status, action, refund = "needs_investigation", "escalate_for_review", Decimal("0")
        parties = [{"party_type": "unknown", "party_id": None}]
    else:
        case_status = rule["case_status"]
        action = rule["recommended_action"]
        refund = money(rule["refund_brl"])
        parties = []
        for party in rule["responsible_parties"]:
            if party["party_type"] == "seller":
                parties += [{"party_type": "seller", "party_id": s} for s in seller_ids]
            else:
                parties.append({"party_type": party["party_type"], "party_id": None})
    # Never refund more than the customer actually paid in this checkout.
    refund = min(refund, captured) if captured else Decimal("0")
    if case_status != "action_required":
        refund = Decimal("0")

    tools = EVIDENCE_PLAN[primary_issue]
    evidence_refs = _refs(findings, tools)

    conflicts: list[dict[str, Any]] = []
    for finding in findings:
        for conflict in finding.conflicts:
            if conflict not in conflicts:
                conflicts.append(conflict)

    refund_lines = []
    if refund > 0:
        target = REFUND_TARGET.get(primary_issue, "order")
        item_ids = facts.get("item_ids", [])
        entity = item_ids[0] if target == "item" and len(item_ids) == 1 else order_id
        refund_lines.append(
            {"reason_code": primary_issue, "amount_brl": float(refund), "entity_id": entity}
        )

    confidence = 0.95
    if primary_issue == "insufficient_evidence":
        confidence = 0.4
    elif conflicts and len(conflicts) > 1:
        confidence = 0.9
    claimed_topics = [c["topic"] for c in request.get("claims", [])]
    if claimed_topics and claimed_topics[0] not in {primary_issue, "requested_full_refund"}:
        confidence = min(confidence, 0.8)

    claim_assessments = []
    for claim in request.get("claims", [])[:5]:
        verdict = _claim_verdict(claim["topic"], primary_issue, refund, action, case_status)
        claim_assessments.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": confidence if verdict != "insufficient_evidence" else 0.6,
                "evidence_refs": evidence_refs,
            }
        )

    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [order_id],
            "item_ids": facts.get("item_ids", []),
            "seller_ids": seller_ids,
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary_issue.upper(), "rank": 1}],
            "responsible_parties": parties[:5],
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(refund),
            "refund_lines": refund_lines,
        },
        "resolution_actions": [action],
    }


def verify(
    output: dict[str, Any],
    order: Finding,
    payment: Finding,
    findings: list[Finding],
    rule: dict[str, Any] | None,
) -> dict[str, bool]:
    consumed = {ev.evidence_ref for f in findings for ev in f.evidence.values()}
    financial = output["financial_resolution"]
    refund = money(financial["recommended_refund_brl"])
    lines_total = sum((money(line["amount_brl"]) for line in financial["refund_lines"]),
                      Decimal("0"))
    status = output["assessment"]["case_status"]
    seller_parties = {
        p["party_id"] for p in output["root_cause_analysis"]["responsible_parties"]
        if p["party_type"] == "seller"
    }
    required = EVIDENCE_PLAN[output["assessment"]["primary_issue"]]
    return {
        "entity_scope": output["affected_entities"]["order_ids"]
        == [order.facts.get("order_id", output["affected_entities"]["order_ids"][0])],
        "evidence_ownership": set(output["evidence_refs"]) <= consumed,
        "required_evidence": len(output["evidence_refs"]) == len(required),
        "claim_linkage": all(
            set(c["evidence_refs"]) <= set(output["evidence_refs"])
            for c in output.get("claim_assessments", [])
        ),
        "money_totals": lines_total == refund,
        "refund_within_paid": refund <= money(payment.facts.get("captured_total") or "0"),
        "status_refund_action": (status == "action_required") == (refund > 0)
        or (rule is not None and money(rule["refund_brl"]) == 0),
        "seller_responsibility": seller_parties <= set(output["affected_entities"]["seller_ids"]),
        "policy_applied": rule is not None,
        "confidence_bounds": 0 <= output["assessment"]["confidence"] <= 1,
    }
