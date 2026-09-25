# L3A Architecture Record

## 1. System overview

`day09 run` đọc `case-set.json` và từng input. CLI ghi `case_received`, sau đó
coordinator trong `solve_case()` giao việc tuần tự cho ba specialist. Mỗi
specialist truy vấn MCP theo `case_id`, trả findings, entities, issues và
evidence refs. Coordinator chỉ đưa ra kết luận được dữ liệu xác nhận; verifier
kiểm tra trước khi CLI validate schema, ghi output và `case_finalized`.

```text
Input → Coordinator → Specialists → Verifier → Output
                         │              │
                         └── MCP ───────┴── Trace
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | Current case | Giao việc, gom evidence, đánh giá claim | L3A output |
| Order/item | Case, gateway | `get_order`, `get_order_items`, `get_sellers` | Order/item/seller findings và handoff |
| Payment | Case, gateway | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | Payment/refund findings và handoff |
| Shipment/policy | Case, gateway | `get_shipment_summary`, `get_policy` | Shipment/policy findings và handoff |
| Verifier | Output, specialist results | Không gọi MCP; kiểm tra bất biến | `verification_completed` |

Tên và tham số tool ở trên đã được đối chiếu với MCP tool discovery. Không dùng
`get_customer_history` khi input không có customer ID được xác nhận; không gọi
tool ngoài nhiệm vụ của từng specialist.

## 3. A2A protocol

Handoff nội bộ có `case_id`, `actor`, `status`, `findings`, `entities`,
`evidence_refs`, `issues` và bản evidence của chính case. Coordinator ghi
`task_assigned` trước mỗi specialist; specialist ghi `tool_result_consumed`
và `handoff`. Luồng tuần tự, mỗi specialist chạy một lần cho một case nên
không có vòng lặp A2A. Gateway có timeout kết nối 30 giây và timeout đọc
300 giây; lỗi tool được ghi vào `issues`, không tự retry vô hạn. Trace chỉ
ghi sự kiện quan sát được và mã quyết định, không ghi khóa hoặc suy luận riêng.

## 4. Evidence lifecycle

`EvidenceGateway` kiểm tra MCP response theo public evidence schema. Tool
luôn nhận `case_id` hiện tại; ref được giữ nguyên từ gateway. Specialist chỉ
emit `tool_result_consumed` sau khi response hợp lệ. Coordinator chỉ dùng ref
trong handoff của chính case; verifier kiểm tra ref trong claim là tập con
của ref output và mọi ref output đã được specialist tiêu thụ. Không tái sử
dụng evidence giữa các case. Khi không có evidence nào, workflow dừng thay
vì tạo output có vẻ hợp lệ nhưng không có provenance.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | Không | Ghi issue, bỏ kết luận thiếu bằng chứng | `handoff` / `*_PARTIAL` |
| Not found | Không | Ghi issue; không điền ID/số tiền phỏng đoán | `handoff` / `*_PARTIAL` |
| Source conflict | Không | Cách ly ID mâu thuẫn, để conflict unresolved | `handoff` / `*_PARTIAL` |
| Invalid specialist result | Không | Verifier từ chối finalize | Không có `verification_completed` |

Không retry tự động: MCP call là read-only nhưng mọi call đều được audit. Không
chuyển missing evidence thành dữ liệu phỏng đoán. Chưa có chính sách retry cho
lỗi mạng ngắn hạn; vận hành có thể chạy lại sau khi gateway ổn định.

## 6. Verification invariants

Verifier kiểm tra `case_id` giữa case và handoff, ref output có nguồn từ
specialist, claim supported có evidence, entity ID và action không trùng,
tổng refund lines bằng recommended refund, và `no_action` không kèm action.
CLI kiểm tra tiếp toàn bộ JSON Schema. Các specialist cách ly ID order/item
mâu thuẫn và không biến claim của khách thành dữ kiện. Khi policy hoặc tiền
thanh toán chưa xác nhận, không đề xuất số tiền hoàn.

## 7. Reproducibility

Workflow deterministic, không dùng model, random seed hoặc concurrent tool
calls. Dependency ranges nằm trong `pyproject.toml`; môi trường chạy nên lưu
`pip freeze` riêng để tái lập chính xác. Chạy `day09 validate-inputs`,
`pytest -q --ignore=tests/test_release_safety.py`, `day09 run`, `day09
validate`, rồi `day09 package --output dist/submission.zip`. Không ghi API
key vào source, trace, output hoặc tài liệu.
