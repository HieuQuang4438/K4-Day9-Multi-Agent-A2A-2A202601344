# Member Role Report — Day 9: Multi Agent A2A

## 1. Thông tin cá nhân

| Thông tin       | Nội dung        |
| --------------- | --------------- |
| Họ và tên       | Chu Quang Hiếu  |
| MSSV            | 2A202601344     |
| Khóa/Lớp        | K4              |
| Vai trò chính   | All             |
| Ngày hoàn thành | 2026-08-05      |

## 2. Vai trò và phạm vi công việc

### Phần việc sở hữu

Vai trò "All": tôi là người triển khai duy nhất, sở hữu toàn bộ pipeline từ tầng dữ liệu đến bài nộp.

| Module/deliverable | File/hàm phụ trách | Input nhận vào | Output bàn giao | Trạng thái |
| ------------------ | ------------------ | -------------- | --------------- | ---------- |
| Tầng dữ liệu | `src/data_store.py` — `DataStore`, `get_store` | 9 file CSV Olist | Bảng đã index theo `order_id`, `customer_id`, `customer_unique_id` | Hoàn thành |
| Tool tất định theo domain | `src/facts.py` — `order_facts`, `customer_facts`, `delivery_facts`, `payment_facts` | `order_id` | Fact bundle: variance giờ, tổng tiền BRL, đối soát | Hoàn thành |
| Rule engine EC_POLICY_V2 | `src/policy.py` — `classify_primary`, `secondary_issues`, `recommended_refund`, `resolution_actions`, `evidence_ids` | Fact bundle | Verdict tất định | Hoàn thành |
| Bộ kiểm chứng | `src/validate.py` — `validate_output` | JSON dự thảo + fact bundle | Danh sách lỗi schema/ID/limit/null | Hoàn thành |
| LLM client | `src/llm.py` — `complete`, `complete_json`, `extract_json` | prompt hệ thống + người dùng | JSON đã parse, token usage, latency | Hoàn thành |
| Runtime 7 agent | `src/agents/` — `base.py`, `tier1.py`, `policy_agent.py`, `verifier_agent.py`, `coordinator.py` | 1 case JSON | Envelope A2A, output JSON, dòng trace | Hoàn thành |
| Trace writer | `src/trace.py` — `TraceWriter` | Bản ghi từng bước agent | `logging/trace.jsonl` một lượt chạy | Hoàn thành |
| Entry point | `src/run.py` | `input/EC_*.json` | 50 file `output/EC_*.json` | Hoàn thành |
| Script kiểm tra bài nộp | `tools/check_submission.py` | `output/` | Báo cáo lỗi + `output.zip` | Hoàn thành |
| Tài liệu kiến trúc | `architecture.md` | — | Sơ đồ agent, quyền truy cập, luồng handoff | Hoàn thành |
| Metadata | `logging/metadata.json`, `src/config.py` | — | Khai báo model, param size, framework, runtime | Hoàn thành |

### Việc hỗ trợ ngoài phạm vi chính

| Hoạt động | Thành viên/module được hỗ trợ | Kết quả |
| --------- | ----------------------------- | ------- |
| Khảo sát model dưới ngưỡng 10B | Toàn bộ pipeline | Phát hiện Gemma 3 ≤10B đã bị gỡ khỏi Google AI API (404), chuyển provider sang OpenRouter |
| Xử lý encoding trên Windows | `requirements.txt`, `tools/check_submission.py` | Sửa lỗi cp1252 làm `pip install -r` và script kiểm tra crash |

## 3. Kết quả theo vai trò

| Nhiệm vụ đã thực hiện | File/hàm/artifact liên quan | Kết quả bàn giao | Cách xác minh |
| --------------------- | --------------------------- | ---------------- | ------------- |
| Đối chiếu 50 `claimed_order_id` với CSV | `src/data_store.py`, `src/facts.py` | 50/50 order tồn tại; 36 `delivered`, 8 `canceled`, 6 `unavailable`; 6 order không có item row | Script đối chiếu pandas trên `olist_orders_dataset.csv` |
| Mã hóa thang ưu tiên EC_POLICY_V2 | `src/policy.py` | Phân bố phủ đủ 6 nhánh: 10/10/8/8/8/6 | Chạy `apply_policy` trên toàn bộ 50 case |
| Dựng runtime 7 agent | `src/agents/` | 8 dòng trace mỗi case: 1 dispatch, 4 tier 1, 1 policy, 1 verifier, 1 written | `python -m src.run --limit 3`, đếm dòng `logging/trace.jsonl` |
| Bộ kiểm chứng 3 tầng | `tools/check_submission.py` | 0 lỗi schema, 0 lệch baseline trên 50/50 case | `python -m tools.check_submission` |
| Chạy song song 50 case | `src/run.py`, `src/llm.py`, `src/trace.py` | 8 case trong 42.2s thay vì ~240s | `python -m src.run --limit 8 --workers 8` |

Một output cụ thể mà phần việc của tôi tạo ra và giúp xác minh:

`tools/check_submission.py` tính lại verdict tất định thẳng từ CSV rồi diff 8 trường được chấm điểm (`primary_issue`, `secondary_issues`, `case_status`, `recommended_refund_brl`, `resolution_actions`, `evidence_ids`, `root_cause_code`, `responsible_parties`) với file thực nộp. Trên cả 50 case, tất cả trùng khớp tuyệt đối, đồng thời `confidence` biến thiên 0.30–0.95 (trung bình 0.86). Hai con số này chứng minh hai điều ngược nhau nhưng đều cần thiết: LLM không làm lệch trường được chấm điểm, nhưng vẫn thực sự suy luận chứ không trả hằng số.

## 4. Giải thích phần kỹ thuật đã thực hiện

### Vấn đề cần giải quyết

Bài toán yêu cầu một hệ multi-agent, nhưng 60% trọng số chấm điểm nằm ở các trường số học: delivery analysis 15%, payment reconciliation 15%, affected entities 15%, root cause và evidence 15%. Ràng buộc thêm là mỗi agent chỉ được dùng model ≤10B tham số.

Mâu thuẫn cốt lõi: một model 4B cộng float trên hàng chục dòng CSV và định dạng lại timestamp sẽ sai lệch không kiểm soát được, nhưng đề lại chấm 0 điểm cho thiết kế "đặt tên nhiều agent mà toàn bộ xử lý nằm trong một prompt". Phần việc của tôi là thiết kế sao cho vừa có phân công/handoff/kiểm chứng thật, vừa không để độ chính xác số học phụ thuộc vào model nhỏ.

### Cách triển khai

**Tách tầng tất định khỏi tầng phán đoán.** Mọi con số lọt vào output JSON được tính bằng Python thuần trong `src/facts.py` và `src/policy.py`. LLM không bao giờ sinh ra một con số để ghi vào file. Phần LLM đảm nhiệm là: chọn phần tử giữ lại khi mảng vượt giới hạn, phát hiện mâu thuẫn giữa các nguồn, chấm `confidence`, và soát chéo output của agent khác.

**Quyền truy cập là ràng buộc cứng, không phải quy ước.** Mỗi agent được khởi tạo với một tool registry đóng trong `Agent.__init__`; gọi tool ngoài danh sách ném `ToolAccessError`. Agent Delivery không chạm được bảng payment. Quan trọng hơn, Policy và Verifier bị cắt hoàn toàn quyền đọc CSV — chúng chỉ suy luận trên bằng chứng do tier 1 bàn giao, nên không thể bịa ra một sự kiện không có trong handoff.

**DAG ba tầng thay vì chuỗi tuần tự.** Bốn agent tier 1 không phụ thuộc lẫn nhau nên chạy song song bằng `ThreadPoolExecutor`; thời gian tier 1 bằng agent chậm nhất chứ không phải tổng bốn. Policy là điểm fan-in duy nhất, chỉ khởi động khi đủ 4 envelope.

**Vòng reject phải có tác dụng thật.** Verifier trả `status = "rejected"` khi bộ kiểm tra tất định tìm thấy lỗi. Nếu Coordinator chỉ gọi lại Policy thì kết quả sẽ y hệt vì Policy là hàm thuần — vòng lặp rỗng. Nên tôi làm khác: khi bị reject, Coordinator **vứt bỏ các lựa chọn mảng do LLM chọn** và dựng lại handoff theo thứ tự nguồn (`_deterministic_handoff`), rồi tính lại verdict. Đây chính là điểm duy nhất LLM ảnh hưởng tới trường được chấm điểm, nên đó cũng là thứ đúng để rút lại khi kiểm chứng thất bại.

**Guard cho lựa chọn của LLM.** Hàm `_guarded_selection` chỉ chấp nhận đề xuất của LLM khi nó là tập con hợp lệ của universe và không vượt giới hạn; sai thì rơi về `universe[:limit]` và gắn cờ `cap_fallback_source_order` vào trace. LLM có quyền quyết định thật, nhưng không có quyền tạo ra ID không tồn tại.

### Input, output và contract

| Thành phần | Mô tả |
| ---------- | ----- |
| Input | `input/EC_0NN.json` với `customer_request.claimed_order_id`; 9 bảng CSV Olist trong `data/` |
| Output | `output/EC_0NN.json` đúng schema đề bài; `logging/trace.jsonl` 8 dòng mỗi case |
| Module phụ thuộc | `src/data_store.py` → `src/facts.py` → `src/policy.py`; `src/config.py` cấp model ID và API key |
| Module sử dụng output | `src/agents/coordinator.py` lắp ráp JSON cuối; `tools/check_submission.py` kiểm tra và đóng zip |
| Điều kiện lỗi cần xử lý | 6 order không có item row (`expected/difference/reconciled` phải `null`, mảng liên quan rỗng); LLM trả JSON bọc code fence; LLM trả JSON hỏng; lỗi mạng/rate limit; Verifier reject 2 vòng liên tiếp |

### Cách xác minh

```bash
python -m src.run --limit 3
python -m tools.check_submission
```

- **Kết quả mong đợi:** mỗi case ghi ra file hợp lệ, `validate_output` trả 0 lỗi, và 8 trường được chấm điểm trùng khớp verdict tất định tính lại từ CSV.
- **Kết quả thực tế:** 50/50 case đạt, `check_submission` báo "không có vấn đề nào". `confidence` phân bố 0.30–0.95, trung bình 0.86. Trace đúng 400 dòng, 8 dòng mỗi case, mỗi case liền một khối. Hai lượt chạy độc lập cho 48/50 file trùng nhau từng byte; hai file lệch chỉ khác `confidence`.
- **Artifact/log:** `output/EC_*.json`, `logging/trace.jsonl`. Không chứa secret; API key chỉ nằm trong `.env` và `.env` đã bị `.gitignore` chặn.

## 5. Một quyết định kỹ thuật quan trọng

- **Bối cảnh:** Đề giới hạn mỗi agent dùng model ≤10B tham số. Nhóm có sẵn Google AI API key, nhưng khi liệt kê model thực tế trên key thì chỉ còn `gemma-4-26b-a4b-it` và `gemma-4-31b-it`. Toàn bộ Gemma 3 dưới 10B (`gemma-3-4b-it`, `gemma-3-12b-it`, `gemma-3n-e4b-it`) trả HTTP 404 — đã bị gỡ khỏi API.

- **Các phương án đã cân nhắc:**
  1. Dùng `gemma-4-26b-a4b-it`: MoE với 4B tham số hoạt động mỗi token nhưng 26B tổng. Chạy được ngay, không cần thiết lập gì thêm.
  2. Chạy `gemma-3-4b` cục bộ qua Ollama: chắc chắn 4B, nhưng phải cài Ollama, tải ~3.3GB, và 50 case × 7 agent chạy trên máy cá nhân.
  3. Đổi provider sang OpenRouter, nơi vẫn phục vụ `google/gemma-3-4b-it`.

- **Phương án đã chọn:** phương án 3 — `google/gemma-3-4b-it` qua OpenRouter.

- **Lý do:** Phương án 1 là rủi ro hard gate. "≤10B" không nói rõ tính tham số tổng hay tham số hoạt động; nếu người chấm tính tổng thì 26B trượt và cả bài mất điểm. Đây là rủi ro nhị phân, không đáng đánh đổi lấy chút tiện lợi. Phương án 2 an toàn về gate nhưng đẩy thời gian chạy lên phụ thuộc phần cứng cá nhân và chất lượng thấp hơn. Phương án 3 vừa chứng minh được ≤10B ngay trong tên model, vừa nhanh, vừa gần như miễn phí.

- **Bằng chứng quyết định phù hợp:** Đo trực tiếp trên key trước khi chốt: `google/gemma-3-4b-it` phản hồi 1.31s, `google/gemma-3n-e4b-it` phản hồi 22.21s cho cùng một prompt — chênh 17 lần, nên loại phương án 3n. Chi phí thực đo qua trường `usage.cost` của OpenRouter là 2.15e-06 USD cho một lệnh gọi thử; ước tính toàn bộ 50 case × 7 agent khoảng 0.05 USD. Model ID được khai trong `src/config.py` và `logging/metadata.json`, không đặt trong `.env`, đúng yêu cầu đề.

## 6. Một lỗi hoặc blocker đã xử lý

- **Triệu chứng/lỗi nguyên văn:** `logging/trace.jsonl` cho số liệu vô lý — 39 dòng trong khi case đang xử lý là `EC_021` (đáng ra khoảng 168 dòng). Trước đó cùng file đã đạt 101 dòng ở `EC_013`, tức số dòng **giảm** khi tiến trình đi tới.

- **Lệnh hoặc bước tái hiện:** chạy `python -m src.run` ở terminal thứ hai trong khi lượt chạy thứ nhất chưa kết thúc.

- **Nguyên nhân gốc:** `TraceWriter` mở file bằng `mode="w"` để thỏa yêu cầu "không append qua các lượt chạy" của đề. Khi có hai tiến trình cùng chạy, tiến trình thứ hai truncate file ngay lúc khởi động, và từ đó hai handle ghi vào cùng một đường dẫn với file offset độc lập, đè lên vùng của nhau. Lock trong `TraceWriter` là `threading.Lock`, chỉ đồng bộ giữa các thread trong một tiến trình, không có tác dụng giữa hai tiến trình. Đây là lỗi vận hành do tôi gây ra, không phải lỗi thiết kế của writer.

- **Cách xử lý:** liệt kê tiến trình bằng `Get-CimInstance Win32_Process`, xác nhận đúng hai tiến trình `-m src.run` khởi động cách nhau 12 phút, dừng cả hai, rồi chạy lại một lượt duy nhất từ đầu. Không chọn cách giữ lại một tiến trình vì trace lúc đó đã lẫn hai lượt và không tách ra được, trong khi đề yêu cầu trace phản ánh đúng một lượt chạy.

- **Cách xác minh sau khi sửa:** viết đoạn kiểm tra tính toàn vẹn đọc lại `trace.jsonl` và khẳng định ba tính chất: đúng 8 dòng mỗi case, thứ tự `case_id` tăng dần, không case nào vượt 8 dòng. Sau khi chạy lại, kết quả là `total lines 142, distinct cases 18, case order monotonic: True, cases with >8 lines: none`. Bài học phụ: khi thấy hai tiến trình python cùng khớp `src.run`, phải kiểm `ParentProcessId` trước khi kết luận — lần thứ hai hóa ra là tiến trình cha `.venv` sinh ra tiến trình con anaconda, tức chỉ một writer thật, và trace hoàn toàn sạch.

- **Điều học được:** một invariant chỉ đúng trong phạm vi nó được bảo vệ. `mode="w"` đảm bảo "một lượt chạy một file" ở mức tiến trình, nhưng không nói gì về nhiều tiến trình. Nếu làm lại, tôi sẽ thêm lock file (`O_EXCL` trên một file `.lock`) để lượt chạy thứ hai thoát ngay với thông báo rõ ràng thay vì âm thầm phá dữ liệu của lượt thứ nhất. Bài học vận hành: khi nghi ngờ dữ liệu bị hỏng, kiểm chứng bằng invariant đo được (số dòng mỗi case) rồi mới hành động, thay vì suy đoán từ tổng số dòng.

## 7. Hiểu biết về luồng end-to-end

> Ghi chú: năm câu hỏi trong mẫu báo cáo hỏi về Crossref, vector index, retrieval quality, freshness monitoring và corrupted/repaired test set. Đây là nội dung của bài lab RAG, không phải bài lab Multi-Agent A2A này — mẫu bị dán nhầm. Tôi trả lời theo các câu hỏi tương ứng của pipeline thực tế đã làm và giữ nguyên thứ tự ý.

**Câu trả lời:**

**1. Dữ liệu đi từ CSV Olist đến output JSON như thế nào?**
`src/data_store.py` nạp 9 bảng CSV một lần duy nhất mỗi tiến trình và dựng index theo `order_id`, `customer_id`, `customer_unique_id`. Coordinator đọc `claimed_order_id` từ file input, phát bốn task envelope cho tier 1. Mỗi agent tier 1 gọi đúng một tool trong `src/facts.py`, nhận về fact bundle đã tính sẵn số, rồi sinh phần diễn giải. Policy gom bốn payload, gọi `apply_policy` lấy verdict tất định và chấm `confidence`. Verifier chạy `validate_output`. Coordinator lắp ráp JSON theo đúng schema đề và ghi ra `output/`.

**2. Bộ kiểm chứng và verdict tất định dùng để đo chất lượng output ra sao?**
`src/policy.py` là ground truth có thể tính lại bất cứ lúc nào từ CSV, độc lập hoàn toàn với LLM. `tools/check_submission.py` tận dụng điều đó: nó tính lại verdict cho từng case rồi diff 8 trường được chấm điểm với file thực nộp. Bất kỳ sai lệch nào giữa hai bên đều là dấu hiệu LLM đã làm hỏng trường được chấm, và bị báo ngay tên case lẫn giá trị lệch.

**3. Kiểm chứng của Verifier khác gì với kiểm tra schema đơn thuần?**
Kiểm tra schema chỉ hỏi "JSON có đúng hình dạng không". Verifier hỏi thêm "nội dung có khớp thực tế không": evidence ID có dựng được từ CSV không, `related_order_ids` có đúng thuộc về khách hàng đó không, order không có item row đã để `null` chưa, `case_status` có nhất quán với `recommended_refund_brl` không. Một file sai hoàn toàn về nghiệp vụ vẫn có thể đúng schema — nên hai lớp này không thay thế nhau.

**4. Vì sao phải so cùng một baseline tất định cho cả lần chạy thử lẫn lượt chạy đầy đủ?**
Vì đó là cách duy nhất tách biệt hai nguồn sai lệch: sai do rule engine hiểu sai đề, và sai do LLM làm nhiễu. Nếu baseline thay đổi giữa các lượt thì khi kết quả lệch sẽ không biết quy trách nhiệm cho bên nào. Giữ baseline cố định và tính lại từ CSV mỗi lần khiến mọi sai lệch chỉ còn một cách giải thích.

**5. Dựa vào artifact và metric nào để coi lượt chạy là thành công?**
Ba điều kiện phải đồng thời đúng: `tools/check_submission.py` báo "không có vấn đề nào" trên đủ 50 case; `logging/trace.jsonl` đúng 8 dòng mỗi case với thứ tự case tăng dần, chứng tỏ một lượt chạy sạch không bị hai tiến trình đè nhau; và `confidence` phải biến thiên giữa các case chứ không phải hằng số, chứng tỏ Policy Agent thực sự suy luận thay vì trả một giá trị mặc định.

## 8. Cam kết của thành viên

Đánh dấu sau khi tự kiểm tra:

- [x] Nội dung báo cáo phản ánh đúng phần việc và mức hiểu của tôi.
- [x] Tôi có thể giải thích luồng end-to-end, không chỉ module mình phụ trách.
- [x] Tôi không ghi "đã chạy thành công" cho phần chưa được kiểm chứng.
- [x] Báo cáo không chứa `.env`, API key, token hoặc secret.
- [x] Báo cáo này không phải bản sao nguyên văn của báo cáo nhóm hoặc báo cáo thành viên khác.

**Họ và tên:** Chu Quang Hiếu
**Ngày xác nhận:** 2026-08-05
