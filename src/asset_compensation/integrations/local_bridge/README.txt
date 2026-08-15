LOCAL BRIDGE CHO CLASSIC OUTLOOK
================================

Hãy tưởng tượng có 3 cái hộp:

  [Outlook] -- chiếc cầu này --> [Product] -- làm xong --> [cửa sổ Trả lời]

Local Bridge chỉ là chiếc cầu. Nó không phải robot gửi mail.
Bạn luôn là người đọc lại và tự bấm nút Gửi trong Outlook.


A. CẦN CÓ GÌ?
---------------

1. Máy Windows.
2. Classic Outlook (Outlook có thanh File / Home / Send-Receive).
3. Python 3.11 trở lên.
4. Outlook đã mở và đã đăng nhập email.

Không cần Microsoft Graph. Không cần App Registration. Không cần client secret.
Không chạy với “New Outlook”. Nếu đang dùng New Outlook, hãy chuyển về Classic Outlook.


B. CÀI MỘT LẦN
---------------

1. Giải nén file ZIP vừa tải từ trang Hướng dẫn của Product.
2. Bấm chuột phải vào install.ps1.
3. Chọn “Run with PowerShell”.
4. Chờ thấy chữ “DA XONG!”.
5. Trên Desktop sẽ có nút “Asset Hub - Outlook Bridge”.

Nếu Windows không cho chạy, mở PowerShell ngay trong thư mục đã giải nén và gõ:

  powershell -ExecutionPolicy Bypass -File .\install.ps1

Chỉ làm bước này với file ZIP tải từ đúng Product của công ty.
Không tải bản do người lạ gửi.


C. DÙNG MỖI NGÀY
-----------------

1. Mở Classic Outlook.
2. Bấm “Asset Hub - Outlook Bridge” trên Desktop.
3. Trong Product, vào phần NganTLT hoặc TranNNB.
4. Bấm tạo mã Local Bridge.
5. Chép mã Ngan vào ô Ngan; chép mã Tran vào ô Tran.
   Hai ô dùng hai mã riêng, giống như hai chiếc chìa khóa cho hai ngăn tủ.
6. Bấm “Kết nối”. Mã chỉ dùng một lần.
7. Bấm “Chọn thư mục”, rồi chọn ĐÚNG thư mục mail cần lấy.
   Chương trình chỉ nhìn ngay trong thư mục đó. Nó không tự chui vào thư mục con.
8. Bấm “Nạp mail mới”.
9. Qua Product kiểm tra và làm dữ liệu như bình thường.

Nếu bạn đang làm NGAN:
10. Dừng ở đây. Ngan chỉ nhận mail và tạo hồ sơ trên Product.
    Ngan không tạo Reply-All quay lại Outlook.

Nếu bạn đang làm TRAN:
10. Khi Product đã tạo draft, quay lại Local Bridge.
11. Bấm “Kiểm tra draft mới”.
12. Chọn một dòng, rồi bấm “Mở Trả lời tất cả”.
13. Outlook mở đúng mail gốc, chèn nội dung và workbook.
14. ĐỌC LẠI. Nếu đúng, chính bạn bấm Gửi.


D. NÚT “TỰ KIỂM TRA 5 PHÚT” LÀ GÌ?
-----------------------------------

- Mặc định nút này TẮT.
- Khi bạn tự bật, cứ 5 phút chương trình nhìn xem có mail mới không.
- Nó chỉ hoạt động khi cửa sổ Local Bridge đang mở.
- Một lần chưa xong thì lần sau sẽ chờ; không chạy chồng lên nhau.
- Muốn dừng, bỏ dấu chọn hoặc đóng chương trình.


E. CHƯƠNG TRÌNH LÀM GÌ VỚI MAIL?
--------------------------------

- Chỉ đọc đúng thư mục bạn chọn.
- Lần đầu chỉ xem mail trong 30 ngày gần nhất và lấy nhiều nhất 20 mail.
- Nếu có hơn 20 mail, bấm "Nạp mail mới" thêm lần nữa. Mỗi lần
  chương trình lấy tiếp 20 mail cũ nhất chưa xử lý, nên không bỏ sót phần còn lại.
- Các lần sau chỉ lấy mail mới trong phiên đang mở.
- Không đánh dấu đã đọc.
- Không chuyển thư mục.
- Không xóa mail.
- Không tự gửi mail.
- Không tải file đính kèm của mail nguồn. Nó chỉ gửi phần chữ/HTML cần phân tích,
  tiêu đề, người gửi/nhận, ngày và Message-ID; tối đa 2 MB mỗi mail.
- Khi mở draft, chương trình tìm mail gốc bằng đúng EntryID + StoreID của Outlook.
  Không tìm thấy đúng mail thì nó dừng, không đoán.
- Workbook draft tối đa 25 MB, chỉ nhận .xlsx hoặc .xlsm.


F. MÃ KẾT NỐI CÓ AN TOÀN KHÔNG?
-------------------------------

- Mã lấy từ Product chỉ dùng một lần.
- Ngan và Tran có mã riêng, nên dữ liệu không đi nhầm ngăn.
- Chìa khóa sau khi kết nối chỉ nằm trong bộ nhớ máy.
- Đóng Local Bridge là chìa khóa biến mất. Lần sau lấy mã mới.
- Chìa khóa không được ghi vào file, không nằm trong link và không hiện trong nhật ký.
- Server thật bắt buộc dùng HTTPS.


G. NẾU OUTLOOK HIỆN CẢNH BÁO?
-----------------------------

Classic Outlook có thể hỏi: “Một chương trình đang muốn truy cập Outlook”.
Chỉ cho phép khi chính bạn vừa bấm nút trong Local Bridge.
Nếu cảnh báo tự xuất hiện lúc bạn không làm gì, hãy bấm từ chối và báo IT.
Chính sách bảo mật của công ty vẫn là luật cao nhất.


H. LỖI THƯỜNG GẶP
-----------------

“Không mở được Classic Outlook”
  -> Mở Classic Outlook trước. New Outlook không dùng được.

“Mã không đúng ô Ngan/Tran”
  -> Tạo lại mã ở đúng phân hệ rồi dán đúng ô.

“Không thấy mail mới”
  -> Kiểm tra bạn đã chọn đúng thư mục chưa. Chương trình không nhìn thư mục con.

“Không tìm thấy mail gốc”
  -> Mail có thể đã bị chuyển/xóa, hoặc Local Bridge đã được đóng rồi mở lại.
     Nạp lại mail trong phiên mới; chương trình không đoán một mail khác.

“Thiếu pywin32”
  -> Chạy lại install.ps1 và kiểm tra mạng/proxy công ty.


I. GỠ CÀI ĐẶT
-------------

1. Đóng Local Bridge.
2. Xóa nút “Asset Hub - Outlook Bridge” trên Desktop.
3. Xóa thư mục:
   %LOCALAPPDATA%\AssetCompensationHub\OutlookBridge

Không có email hay token nào được Local Bridge cất trong thư mục đó.
