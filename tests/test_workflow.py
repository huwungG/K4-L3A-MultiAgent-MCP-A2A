from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import classify, solve_case

ROOT = Path(__file__).resolve().parents[1]
ORDER_ID = "0123456789abcdef0123456789abcdef"
CASE_ID = "L3A_CASE_T01"


def _ref(name: str) -> str:
    return f"ev_{name.replace('_', '-'):->24}"


def _evidence(tool: str, domain: str, data: Any) -> dict[str, Any]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": _ref(tool),
        "result_hash": "sha256:" + "0" * 64,
        "domain": domain,
        "data": data,
        "warnings": [],
    }


POLICY = {
    "currency": "BRL",
    "policy_version": "EC_POLICY_V1",
    "rules": {
        "late_delivery_seller": {
            "case_status": "action_required",
            "recommended_action": "refund_freight",
            "refund_brl": 18.0,
            "responsible_parties": [{"party_id": "seller-template", "party_type": "seller"}],
        },
    },
}


class FakeGateway:
    def __init__(self) -> None:
        item = {
            "order_id": ORDER_ID, "order_item_id": "item-1", "product_id": "p-1",
            "seller_id": "seller-1", "price": "79.00", "freight_value": "18.00",
        }
        self.responses = {
            "get_policy": _evidence("get_policy", "policy", POLICY),
            "get_order": _evidence("get_order", "order", {
                "order_id": ORDER_ID, "customer_id": "c-1", "order_status": "delivered",
                "order_purchase_timestamp": "2018-02-19T09:00:00-03:00",
                "order_approved_at": "2018-02-19T10:00:00-03:00",
                "order_delivered_carrier_date": "2018-02-26T09:00:00-03:00",
                "order_delivered_customer_date": "2018-03-05T09:00:00-03:00",
                "order_estimated_delivery_date": "2018-03-01T09:00:00-03:00",
            }),
            "get_order_items": _evidence("get_order_items", "item", [
                {**item, "shipping_limit_date": "2018-02-22T09:00:00-03:00"},
                # Row from another checkout, outside the case window: must be ignored.
                {**item, "shipping_limit_date": "2018-06-22T09:00:00-03:00"},
            ]),
            "get_sellers": _evidence("get_sellers", "seller", [{"seller_id": "seller-1"}]),
            "get_payment_timeline": _evidence("get_payment_timeline", "payment", {
                "order_id": ORDER_ID, "payments": [],
                "events": [{"order_id": ORDER_ID, "event_at": "2018-02-19T10:00:00-03:00",
                            "event_type": "captured", "amount_brl": "18.00",
                            "status": "confirmed"}],
            }),
            "get_shipment_summary": _evidence("get_shipment_summary", "shipment", {
                "order_id": ORDER_ID, "order_status": "delivered",
                "delivered_carrier_at": "2018-02-26T09:00:00-03:00",
                "delivered_customer_at": "2018-03-05T09:00:00-03:00",
                "estimated_delivery_at": "2018-03-01T09:00:00-03:00",
                "shipping_limits": [], "events": [],
            }),
        }
        self.calls: list[tuple[str, str]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id))
        if tool_name not in self.responses:
            raise RuntimeError(f"MCP tool {tool_name} failed: not found")
        return self.responses[tool_name]


def test_classify_prefers_order_status_over_payment_signals() -> None:
    order = {"order_status": "canceled", "order_total": "89.00"}
    payment = {"captured_total": "79.00", "captures": ["79.00"], "refund_status": "failed"}
    assert classify(order, payment, {}) == "canceled_order_paid"


def test_classify_split_vs_duplicate() -> None:
    order = {"order_status": "delivered", "order_total": "89.00"}
    split = {"captured_total": "89.00", "captures": ["44.50", "44.50"]}
    duplicate = {"captured_total": "128.00", "captures": ["64.00", "64.00"]}
    assert classify(order, split, {}) == "valid_split_payment"
    assert classify(order, duplicate, {}) == "duplicate_charge"
    assert classify(order, {"captured_total": "89.00", "captures": ["89.00"]}, {}) == (
        "unsupported_claim"
    )


def test_solve_case_late_seller_end_to_end(tmp_path: Path) -> None:
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    case = {
        "case_id": CASE_ID,
        "opened_at": "2018-03-10T09:00:00-03:00",
        "customer_request": {
            "language": "vi", "message": "test", "claimed_order_id": ORDER_ID,
            "claims": [{"claim_id": "c-a", "topic": "late_delivery_seller"},
                       {"claim_id": "c-b", "topic": "requested_full_refund"}],
        },
        "policy_version": "EC_POLICY_V1",
    }
    gateway = FakeGateway()
    output = asyncio.run(solve_case(case, gateway, trace))
    contracts.validate_output(output, "output")

    assert output["assessment"]["primary_issue"] == "late_delivery_seller"
    assert output["financial_resolution"]["recommended_refund_brl"] == 18.0
    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "seller", "party_id": "seller-1"}
    ]
    assert output["affected_entities"]["item_ids"] == ["item-1"]
    assert {case_id for _, case_id in gateway.calls} == {CASE_ID}

    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    types = {event["event_type"] for event in events}
    assert {"task_assigned", "handoff", "tool_result_consumed", "policy_decided",
            "verification_completed"} <= types
    consumed = {ref for e in events if e["event_type"] == "tool_result_consumed"
                for ref in e["evidence_refs"]}
    assert set(output["evidence_refs"]) <= consumed
    verification = next(e for e in events if e["event_type"] == "verification_completed")
    assert verification["decision_code"] == "PASS"
