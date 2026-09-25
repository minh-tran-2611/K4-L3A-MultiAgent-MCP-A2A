# TODO cho nhóm L3A (4 người, gồm nhóm trưởng)

> Dùng tài liệu này để chia việc và theo dõi tiến độ. Các ô `[ ]` được đánh dấu `[x]` khi đã có kết quả kiểm chứng được. Phân công **con người** dưới đây là đề xuất của nhóm; repo không quy định cứng bốn vai trò. README gợi ý sáu vai trò **agent**: coordinator, order/item, payment, shipment, policy và verifier.

## 0. Mục tiêu và nguồn chuẩn

- Xử lý đủ **100 case L3A** từ `case-set.json` và `inputs/<case_id>.json`.
- Với mỗi case, đọc khiếu nại, lấy dữ liệu có thẩm quyền qua MCP, phối hợp các agent và xuất `outputs/<case_id>.json` đúng `contracts/schemas/l3a-output-v2.schema.json`.
- Ghi `traces/trace.jsonl` đúng `contracts/schemas/trace-event-v1.schema.json` để thấy quá trình giao việc, dùng evidence, bàn giao và xác minh.
- Nộp `dist/submission.zip` được tạo bằng `day09 package`; ZIP chỉ có `manifest.json`, `trace.jsonl` và các file trong `outputs/`.

**Đọc trước khi làm:** `README.md`, `ARCHITECTURE.md`, `contracts/schemas/l3a-output-v2.schema.json`, `contracts/schemas/mcp-evidence-response-v1.schema.json`, `contracts/schemas/trace-event-v1.schema.json` và `contracts/scoring/scoring-policy-v2.json`. Không sửa `contracts/` để hợp thức hóa output.

**Quy tắc bắt buộc:** lời kể của khách hàng là claim cần kiểm tra, không phải dữ kiện đã xác nhận. Không đoán dữ liệu thiếu, không tạo/sửa `evidence_ref`, không dùng evidence của case khác. Mọi MCP call dùng đúng `case_id`. Chỉ dẫn evidence thật sự hỗ trợ kết luận.

### Phân biệt ba nguồn dữ liệu

| Nguồn | Dùng để làm gì? | Có phải tải về để chạy lab? |
| --- | --- | --- |
| Dataset Olist trên Kaggle | Tài liệu tham khảo về bối cảnh thương mại điện tử; README chỉ ghi “tham khảo dữ liệu tại” | **Không bắt buộc** theo README; không lấy dữ liệu Kaggle làm bằng chứng cho case |
| ZIP **input L3A** từ GitHub Release | Chứa `case-set.json` và 100 file `inputs/<case_id>.json`: các case/claim mà workflow phải xử lý | **Bắt buộc** trên máy chạy `day09 validate-inputs`, `day09 run`, `day09 validate`, `day09 package` |
| MCP Evidence Gateway | Trả dữ liệu có thẩm quyền về order, payment, shipment, policy… cùng `evidence_ref`; các call được audit | Không tải thành dataset; agent truy vấn bằng tool với đúng `case_id` |

Nhóm trưởng chịu trách nhiệm lấy ZIP input và bảo đảm cả nhóm có **cùng phiên bản input** khi cần phát triển/test. Không bắt từng thành viên tải Kaggle. Người viết agent cần xem case mẫu và có cách gọi MCP để kiểm tra phần mình; có thể dùng bản ZIP input cùng phiên bản trên máy riêng hoặc làm trên môi trường chung. Không commit input vào Git vì repo đã ignore thư mục này.

## 1. Phân công và file sở hữu

| Người | Trách nhiệm | File chính | Bàn giao |
| --- | --- | --- | --- |
| **Nhóm trưởng** | Đăng ký/config/input; chốt giao diện giữa các agent; coordinator, verifier, tích hợp, chạy cuối và nộp | Sửa `src/student_agent/workflow.py`, `ARCHITECTURE.md`; có thể tạo `src/student_agent/verifier.py` và test tương ứng | `solve_case()` trả về một output L3A hợp lệ cho từng case; trace và ZIP hợp lệ |
| **Thành viên 1** | Order, item, seller: xác minh trạng thái đơn, mặt hàng, seller và ID liên quan | Tạo `src/student_agent/order_agent.py`, `tests/test_order_agent.py` | Findings có nguồn MCP, entity ID và evidence refs cho kết luận về order/item/seller |
| **Thành viên 2** | Payment, refund: xác minh số tiền, split payment, thanh toán lệch/trùng và tiến độ hoàn tiền | Tạo `src/student_agent/payment_agent.py`, `tests/test_payment_agent.py` | Findings về payment/refund, các dòng tiền và evidence refs; không tự suy ra tiền hoàn khi thiếu dữ liệu |
| **Thành viên 3** | Shipment, policy: xác minh giao hàng, nguyên nhân chậm, trách nhiệm và chính sách áp dụng | Tạo `src/student_agent/shipment_policy_agent.py`, `tests/test_shipment_policy_agent.py` | Findings về shipment/policy, bên chịu trách nhiệm và evidence refs |

Tên file mới ở bảng là **quy ước nhóm đề xuất**, không phải yêu cầu của lab. Mỗi người ưu tiên sửa file mình sở hữu; nhóm trưởng ghép qua `workflow.py` để tránh xung đột. Khi cần đổi giao diện chung, báo cả nhóm trước.

## 2. Giai đoạn A — chuẩn bị (nhóm trưởng)

- [ ] Đăng ký đủ bốn người ở `/register` trên Competition Workspace; lưu Team API Key dạng `sk-team-...`.
- [ ] Giữ nguyên tên repo khi fork theo README. Không commit `.env`, API key, input/output hoặc ZIP; `.gitignore` đã loại các file này khỏi Git.
- [ ] Trên Windows PowerShell, cài môi trường từ thư mục gốc repo:

  ```powershell
  py -3.11 -m venv .venv
  .\.venv\Scripts\Activate.ps1
  python -m pip install -e ".[dev]"
  Copy-Item .env.example .env
  ```

- [ ] Điền `COMPETITION_API_URL`, `COMPETITION_TEAM_API_KEY` và `MCP_ENDPOINT` thật vào `.env`. Các địa chỉ trong README là ví dụ; dùng địa chỉ được cấp cho lớp/team. Không gửi key qua commit hoặc log.
- [ ] Khi repo còn sạch và **chưa tải input/output**, chạy `pytest -q` và `day09 --help` để kiểm tra bộ khung.
- [ ] Tải ZIP input **L3A** từ GitHub Release theo README, giải nén vào root repo sao cho có `case-set.json` và `inputs/*.json`; chạy `day09 validate-inputs`. Lệnh này yêu cầu đúng 100 case, đúng variant `l3a` và tập file khớp manifest.
  - Release của **repo gốc**: https://github.com/VinUni-AI20k/K4-L3A-MultiAgent-MCP-A2A/releases/tag/v1 — tải file **`l3a-inputs-v1.zip`** trong mục “Download input”. Fork của nhóm có thể không hiển thị Release này.
- [ ] Chạy `day09 mcp-tools`, chia sẻ **danh sách tên tool và mô tả/đối số đã xác minh** cho cả nhóm. Không đoán tên tool hay cấu trúc `data` của MCP trước khi xem dữ liệu thật.
- [ ] Chọn 2–3 case mẫu có tình huống khác nhau để cả nhóm cùng hiểu input và evidence; không xem customer message là ground truth.

**Lưu ý kiểm thử:** `tests/test_release_safety.py` cố ý xác nhận bản repo phát hành không có `case-set.json`, input và output. Sau khi tải dữ liệu hoặc chạy workflow, `pytest -q` toàn bộ sẽ fail ở test này theo thiết kế. Trong giai đoạn phát triển với dữ liệu thật, dùng `pytest -q --ignore=tests/test_release_safety.py` cho các test code; không xóa dữ liệu thật chỉ để qua test phát hành.

## 3. Giai đoạn B — chốt giao diện trước khi ba người code sâu

Nhóm trưởng tạo skeleton hoặc gửi hợp đồng hàm này cho cả nhóm; chỉ đổi khi cả nhóm đồng ý:

```python
async def analyze_order(case, gateway, trace) -> dict: ...
async def analyze_payment(case, gateway, trace) -> dict: ...
async def analyze_shipment_policy(case, gateway, trace) -> dict: ...
```

- [ ] Mỗi hàm nhận **case hiện tại**, `EvidenceGateway` và `TraceWriter`; trả về kết quả của **chính case đó**. Thống nhất ít nhất các khóa nội bộ: `case_id`, `findings`, `entities`, `evidence_refs`, `issues`. Đây là giao diện nội bộ, **không phải output schema để nộp**.
- [ ] `findings` phải ghi rõ kết luận nào dựa vào bằng chứng nào; `issues` ghi thiếu evidence, lỗi tool hoặc nguồn dữ liệu mâu thuẫn. Không biến lỗi/thiếu dữ liệu thành một sự kiện đã xác nhận.
- [ ] Chốt actor name trong trace, ví dụ `order-agent`, `payment-agent`, `shipment-policy-agent`, `coordinator`, `verifier`; dùng nhất quán trong code và `ARCHITECTURE.md`.
- [ ] Chốt tool nào từng agent được phép gọi sau khi discovery. Khi cần thêm tool, báo nhóm trưởng để cập nhật thiết kế và tránh truy vấn không liên quan.
- [ ] Chốt cách bàn giao: coordinator ghi `task_assigned`; agent ghi `tool_result_consumed` cho evidence đã dùng rồi `handoff`; verifier ghi `verification_completed`. `cli.py` đã ghi `case_received` và `case_finalized`.

## 4. Giai đoạn C — việc từng người

### Thành viên 1 — order/item/seller

- [ ] Đọc input mẫu để lấy order/item/seller ID được cung cấp; xác minh các ID qua MCP và giữ đúng scope case.
- [ ] Phân biệt trạng thái order thực tế với claim của khách; trả lại entity ID đã xác minh và dấu hiệu hủy đơn, hết hàng hoặc vấn đề seller khi có bằng chứng.
- [ ] Gắn `evidence_ref` vào từng finding, emit `tool_result_consumed` khi dùng kết quả MCP; báo `issues` khi thiếu dữ liệu hoặc nguồn xung đột.
- [ ] Viết test với gateway/trace giả cho ít nhất một case có evidence và một case thiếu/mâu thuẫn evidence. Không tạo evidence giả trong **output nộp**; test có thể dùng fixture giả để kiểm tra logic.

### Thành viên 2 — payment/refund

- [ ] Xác minh payment reference, các khoản đã trả, split payment, nghi vấn trùng/lệch và trạng thái refund bằng MCP.
- [ ] Tính tiền bằng dữ liệu được xác minh; nêu rõ thành phần và entity liên quan để nhóm trưởng ghép `financial_resolution` (`currency: BRL`, tổng tiền hoàn, các `refund_lines`).
- [ ] Tránh hoàn trùng, hoàn vượt số tiền có căn cứ hoặc kết luận "đã hoàn" chỉ từ lời khách.
- [ ] Gắn evidence cho từng kết luận, emit trace; viết test cho tình huống thanh toán hợp lệ và tình huống cần xử lý/thiếu evidence.

### Thành viên 3 — shipment/policy

- [ ] Xác minh shipment ID, mốc giao hàng, chậm giao và bên liên quan bằng MCP.
- [ ] Tra policy đúng điều kiện case, chỉ đề xuất nguyên nhân/trách nhiệm/hướng xử lý khi evidence hỗ trợ; phân biệt lỗi seller, logistics và trường hợp chưa đủ bằng chứng.
- [ ] Gắn evidence, emit trace; báo mâu thuẫn nguồn thay vì âm thầm chọn một nguồn.
- [ ] Viết test cho ít nhất một tình huống giao trễ có căn cứ và một tình huống policy/ship evidence chưa đủ.

### Nhóm trưởng — coordinator, verifier, kiến trúc

- [ ] Thay `NotImplementedError` trong `solve_case()` bằng luồng giao việc → gọi ba agent → gom findings → verifier → tạo output.
- [ ] Map kết quả vào toàn bộ field bắt buộc của L3A: `schema_version`, `case_id`, `assessment`, `affected_entities`, `root_cause_analysis`, `evidence_refs`, `data_conflicts`, `financial_resolution`, `resolution_actions`. `claim_assessments` là field tùy chọn nhưng nên dùng khi input có nhiều claim cần đánh giá riêng.
- [ ] Verifier kiểm tra trước khi trả output: đúng `case_id`; entity và evidence thuộc case; claim có evidence tương ứng; tổng `refund_lines` bằng `recommended_refund_brl`; trách nhiệm, `case_status`, hành động và tiền hoàn nhất quán; confidence trong `[0,1]`; không có ID/action trùng hoặc field ngoài schema.
- [ ] Ghi trace cho giao việc, handoff và verification. Trace chỉ chứa sự kiện/decision code quan sát được; không ghi key, prompt bí mật hay chain-of-thought.
- [ ] Cập nhật `ARCHITECTURE.md`: luồng hệ thống, quyền gọi tool theo actor, giao diện/handoff A2A theo `case_id`, timeout và retry có giới hạn, vòng đời evidence, xử lý lỗi, bất biến verifier, cách tái lập lần chạy.
- [ ] Viết test cho tích hợp `solve_case()` bằng gateway giả để phát hiện lỗi ghép dữ liệu/trace trước khi chạy 100 case thật.

## 5. Giai đoạn D — tích hợp và kiểm tra chung

- [ ] Review từng phần đã bàn giao: tên hàm đúng giao diện; không hardcode tên tool chưa xác minh; không tạo `evidence_ref`; không dùng evidence ngoài case; có test ý nghĩa.
- [ ] Chạy test code: `pytest -q --ignore=tests/test_release_safety.py` (khi input/output thật còn trong repo).
- [ ] Chạy `day09 run` và `day09 validate`. `day09 run` **xóa mọi `outputs/*.json` và `traces/trace.jsonl` cũ trước khi chạy**, vì vậy chỉ nhóm trưởng thực hiện lần chạy tích hợp/cuối và cần giữ bản kết quả cần so sánh ở nơi riêng.
- [ ] Kiểm tra đủ 100 output, mỗi file đúng `case_id`; xem vài case thuộc nhiều loại nghiệp vụ và kiểm tra thủ công tính hợp lý của kết luận, tiền hoàn, trách nhiệm, action và evidence.
- [ ] Kiểm tra trace có thứ tự `case_received` → giao việc/handoff → `verification_completed` → `case_finalized`; evidence dùng trong output có dấu vết tiêu thụ tương ứng. Schema hợp lệ chưa đủ để đạt điểm semantic/evidence.
- [ ] Nếu MCP timeout/not found, chỉ retry hữu hạn khi an toàn; giữ lại lỗi/thiếu evidence trong luồng xử lý. Không điền giá trị đoán để làm output trông đầy đủ.

## 6. Giai đoạn E — đóng gói và nộp (nhóm trưởng)

- [ ] Chạy `day09 validate` ngay trước khi đóng gói.
- [ ] Chạy `day09 package --output dist/submission.zip`.
- [ ] Kiểm tra ZIP chỉ có `manifest.json`, `trace.jsonl`, `outputs/<case_id>.json` cho đúng 100 case. Không có source, input, `.env`, API key hay debug log.
- [ ] Upload `dist/submission.zip` tại Competition Workspace `/l3a` theo README; lưu lại phản hồi/điểm công khai để nhóm sửa các lỗi có căn cứ nếu còn thời gian.

## Tiêu chí ưu tiên khi cần sửa bài

Theo `contracts/scoring/scoring-policy-v2.json`: semantic **45%**, evidence **15%**, provenance **15%**, consistency **10%**, schema **5%**, calibration **5%**, workflow **5%**. L3A không tính điểm efficiency trực tiếp, nhưng MCP call vẫn được audit. Case có thể nhận **0 điểm** nếu sai `case_id`, schema không thể chấm, thiếu evidence bắt buộc hoặc dùng evidence ref không tồn tại/sai team, run, case. Ưu tiên sửa lỗi nghiệp vụ và bằng chứng trước khi tinh chỉnh hình thức trace.
