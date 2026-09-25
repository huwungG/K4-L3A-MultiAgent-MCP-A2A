# L3A Architecture Record

Tài liệu mô tả quyết định có thể kiểm chứng trong source (`src/student_agent/`). Không chứa prompt bí mật hay chain-of-thought: toàn bộ workflow là deterministic, không dùng LLM.

## 1. System overview

```text
inputs/<case_id>.json
        │  case_received
        ▼
  ┌─────────────┐  task_assigned / handoff (A2ABus, correlated by case_id)
  │ Coordinator │──────────────────────────────────────────────────────┐
  └─────┬───────┘                                                      │
        │ 1. assign:load_policy        ──► policy-agent     ──MCP──► get_policy
        │ 2. assign:verify_order_scope ──► order-item-agent ──MCP──► get_order, get_order_items, get_sellers
        │ 3. assign:reconcile_payments ──► payment-agent    ──MCP──► get_payment_timeline, get_refund_timeline
        │ 4. assign:check_delivery     ──► shipment-agent   ──MCP──► get_shipment_summary
        │ 5. classify(primary_issue)   (decision table trên facts đã scope)
        │ 6. assign:apply_policy       ──► policy-agent  → policy_decided
        │ 7. draft output (chỉ cite evidence cần cho kết luận)
        │ 8. assign:verify_output      ──► verifier      → verification_completed
        ▼
outputs/<case_id>.json  +  traces/trace.jsonl (case_finalized)
```

Mọi specialist emit `tool_result_consumed` cho từng MCP response đã dùng; mỗi message A2A được trace thành `task_assigned` (intent `assign:*`) hoặc `handoff`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator (`workflow.solve_case`) | Case input (claimed order, claims, `opened_at`, policy version) | Giao việc, gom findings, chọn `primary_issue` bằng decision table `classify()` — không tin claim của khách | Draft output → verifier; `case_finalized` (CLI) |
| Order/item (`OrderItemAgent`) | `claimed_order_id` | Xác minh order tồn tại; lấy item/seller; chọn 1 row có thẩm quyền cho mỗi `order_item_id` (shipping limit đầu tiên trong `[purchase, opened_at]`); tính `order_total` | `order_scoped` / `order_not_found` + facts |
| Payment (`PaymentAgent`) | Order facts đã scope | Lọc event trong cửa sổ case và cửa sổ checkout (≤24h sau approve); dedupe row trùng; phát hiện mismatch, refund pending/failed | `payments_reconciled` + captures, refund status |
| Shipment (`ShipmentAgent`) | Order facts đã scope | So `delivered_customer` vs `estimated`, `delivered_carrier` vs shipping limit; đánh dấu event mâu thuẫn với order row | `delivery_checked` + `delivered_late`, `seller_late_handoff` |
| Policy (`PolicyAgent`) | `policy_version`, `primary_issue` | Load policy; map issue → `case_status`, action, `refund_brl`, responsible party | `policy_loaded`, `policy_decided`, `policy_applied` |
| Verifier (`workflow.verify`) | Draft output + findings | Kiểm tra invariants (mục 6); hạ confidence nếu có check fail | `verification_completed`, `verified` |

Quyền gọi tool (enforced bởi `Specialist.allowed_tools`, gọi sai → `PermissionError`):

| Actor | Tools |
| --- | --- |
| order-item-agent | `get_order`, `get_order_items`, `get_sellers` |
| payment-agent | `get_payment_timeline`, `get_refund_timeline` |
| shipment-agent | `get_shipment_summary` |
| policy-agent | `get_policy` |
| coordinator, verifier | không gọi MCP |

`get_customer_history`, `get_product_context`, `get_order_payments` không được dùng: không cần cho kết luận (payment timeline đã bao gồm payment rows) và tránh cite domain không liên quan.

## 3. A2A protocol

- Envelope (`a2a.A2AMessage`): `message_id` (`<case_id>-mNN`), `case_id`, `sender`, `recipient`, `intent`, `payload` (facts có cấu trúc), `evidence_refs`, `hop`.
- Correlation: một `A2ABus` cho mỗi case; mọi message và trace event mang đúng `case_id` của case đó.
- Handoff: specialist chỉ handoff về coordinator sau khi đã consume evidence; payment/shipment chỉ được giao việc khi order agent trả `order_scoped`.
- Chống vòng lặp: luồng tuyến tính, không có agent nào gửi lại cho chính mình; `MAX_HOPS = 16` → vượt quá thì raise.
- Timeout: mỗi MCP call có `TOOL_TIMEOUT_SECONDS = 60`, retry 1 lần khi timeout.
- Trace chỉ chứa event quan sát được (`decision_code` = intent / action / PASS), không chứa suy luận.

## 4. Evidence lifecycle

1. `EvidenceGateway.call` validate response theo `mcp-evidence-response-v1` (schema, `evidence_ref`, `result_hash`, `domain`).
2. Specialist lưu `Evidence(tool_name, evidence_ref, domain, data)` trong `Finding` của case hiện tại và emit `tool_result_consumed` (actor, tool, ref).
3. Coordinator chọn evidence theo `EVIDENCE_PLAN[primary_issue]` — chỉ cite các tool thực sự hỗ trợ kết luận (ví dụ `late_delivery_seller` cite order + shipment + items + sellers + policy; `duplicate_charge` cite order + payment timeline + policy).
4. `claim_assessments[*].evidence_refs` là tập con của `evidence_refs` output.
5. Evidence refs không bao giờ được tạo/sửa; `Finding` được tạo mới cho mỗi case nên không thể dùng chéo case.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | 1 lần / call (60s) | Ghi `missing`, không cite tool đó | Không có `tool_result_consumed` cho tool đó |
| Transport lỗi (connect/read) | Có, tối đa 4 lần / case, reconnect session, backoff 2s·n | Trace của lần thử lỗi bị huỷ (buffer theo case) nên mỗi case chỉ trace 1 lần | Log stderr `retry <case>` |
| Not found (tool error, ví dụ không có refund) | Không | Coi như không có dữ liệu; order không tồn tại → `insufficient_evidence`, `needs_investigation` | `order_not_found` handoff |
| Source conflict | Không | Chọn nguồn có thẩm quyền: order row > shipment event; row thuộc checkout của case > row ngoài cửa sổ | `data_conflicts[*].resolution_code` |
| Invalid specialist result | Không | Verifier hạ confidence ≤ 0.5 | `verification_completed` `PASS_WITH_WARNINGS` + attribute `failed` |
| Contract/schema lỗi | Không | Dừng run (không nộp output sai schema) | — |

Không bao giờ chuyển missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

`workflow.verify()` kiểm tra trước khi finalize:

- `entity_scope`: `order_ids` đúng bằng order đã xác minh.
- `evidence_ownership`: mọi `evidence_refs` output đều đã được consume trong case này.
- `required_evidence`: đủ evidence theo `EVIDENCE_PLAN`.
- `claim_linkage`: evidence của claim ⊆ evidence output.
- `money_totals`: tổng `refund_lines` = `recommended_refund_brl`.
- `refund_within_paid`: refund ≤ số tiền đã capture trong checkout.
- `status_refund_action`: `action_required` ⇔ refund > 0 (trừ rule policy có refund 0).
- `seller_responsibility`: seller chịu trách nhiệm ∈ `affected_entities.seller_ids`.
- `policy_applied`, `confidence_bounds` ∈ [0, 1].
- Schema output được CLI validate lại trước khi ghi file.

## 7. Decision table (`classify`)

Ưu tiên tín hiệu cụ thể nhất, trên dữ liệu đã scope vào cửa sổ `[purchase, opened_at]`:

1. `order_status = canceled` + đã capture → `canceled_order_paid`
2. `order_status = unavailable` + đã capture → `unavailable_order_paid`
3. Refund event `failed` → `refund_failed`; `pending` → `refund_pending`
4. `reconciliation_mismatch` open → `payment_mismatch`
5. Giao trễ (delivered > estimated): carrier nhận hàng sau shipping limit → `late_delivery_seller`, ngược lại → `late_delivery_logistics`
6. ≥ 2 capture: tổng = order total → `valid_split_payment`; trùng số tiền → `duplicate_charge`
7. Còn lại → `unsupported_claim`

Status, action, refund và loại bên chịu trách nhiệm lấy từ policy `EC_POLICY_V1`; `party_id` của seller lấy từ item của chính order (không lấy seller mẫu trong policy). Refund bị chặn trên bởi số tiền đã capture.

## 8. Reproducibility

- Không dùng LLM, không có randomness trong quyết định (chỉ `event_id` trace là random).
- Python ≥ 3.11; dependency theo `pyproject.toml` (`mcp` 2.x, `httpx2`, `jsonschema`).
- Concurrency: tuần tự, 1 MCP session, các case chạy lần lượt.
- Lệnh chạy:

```bash
day09 validate-inputs
day09 run
day09 validate
day09 package --output dist/submission.zip
```

- Cấu hình qua `.env` (`COMPETITION_API_URL`, `COMPETITION_TEAM_API_KEY`, `MCP_ENDPOINT`); API key không được ghi vào output/trace (kiểm tra bởi `validate_artifacts`).
