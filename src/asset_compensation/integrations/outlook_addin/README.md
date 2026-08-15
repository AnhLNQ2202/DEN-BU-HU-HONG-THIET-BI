# Outlook Office.js Add-in

Gói add-in này cho phép người dùng chủ động đưa **email đang mở** vào Asset
Compensation Hub và, với luồng TranNNB, mở cửa sổ **Reply All** trên chính email
nguồn. Add-in không quét mailbox, không dùng Microsoft Graph và không có thao tác
gửi mail.

## Cài đặt cho người dùng

1. Mở Product, vào phần **Kết nối Outlook** và tải file `manifest.xml` dành cho
   môi trường hiện tại.
2. Trong Outlook, mở **Get Add-ins / Tải phần bổ trợ**. Có thể mở nhanh trang
   sideload bằng <https://aka.ms/olksideload>.
3. Chọn **My add-ins / Phần bổ trợ của tôi** → **Add a custom add-in / Thêm phần
   bổ trợ tùy chỉnh** → **Add from File / Thêm từ Tệp**.
4. Chọn file `manifest.xml`, chấp nhận hộp thoại cài đặt, sau đó đóng và mở lại
   Outlook nếu nút **Đưa vào Product** chưa xuất hiện.

Classic Outlook có thể cache manifest lâu. Microsoft ghi nhận việc add-in sideload thủ công
có thể mất tới 24 giờ để hiển thị; thường đóng/mở Outlook sẽ nhanh hơn.

Tài liệu Microsoft: <https://learn.microsoft.com/office/dev/add-ins/outlook/sideload-outlook-add-ins-for-testing>

## Sử dụng

1. Trên Product, tạo mã ghép nối cho đúng phân hệ **NganTLT** hoặc **TranNNB** và
   chọn loại client **Outlook Add-in**.
2. Mở email báo mất/hư hỏng trong Outlook, bấm **Đưa vào Product**, dán mã và
   bấm **Ghép nối**.
3. Bấm **Đưa email này vào Product**. Add-in xuất duy nhất email đang mở thành
   EML và nạp vào pipeline kiểm tra hiện có của Product.
4. Xử lý case trên Product như bình thường.
5. Với TranNNB, sau khi Product tạo draft package, quay lại đúng email nguồn và bấm
   **Làm mới** → **Mở Reply All + workbook**.
6. Outlook chỉ mở form Reply All. Người dùng phải kiểm tra người nhận, nội dung,
   workbook và tự bấm **Send**.

Draft chỉ được mở nếu `source_eml_handle` của package khớp tuyệt đối với
handle mà add-in đã lưu cho item Outlook hiện tại trong `sessionStorage`.

## Yêu cầu và giới hạn Office.js

- HTTPS hợp lệ là bắt buộc; task pane, API và manifest phải cùng origin.
- `getAsFileAsync` để xuất EML yêu cầu **Mailbox 1.14** và quyền `ReadItem`.
- `displayReplyAllFormAsync` yêu cầu **Mailbox 1.9** và quyền `ReadItem`.
- Đính workbook từ Base64 trong `ReplyFormAttachment` yêu cầu **Mailbox 1.15**. Máy chỉ
  hỗ trợ 1.14 vẫn nạp EML được, nhưng UI sẽ khóa nút Reply All kèm workbook và
  hướng dẫn dùng Local Bridge hoặc Product. Không dùng URL workbook công khai để lách
  giới hạn này.
- Giới hạn phía client khớp với companion API: EML tối đa 2 MB, workbook tối đa
  25 MB, HTML draft tối đa 32 KB.

Tài liệu API:

- <https://learn.microsoft.com/javascript/api/outlook/office.messageread>
- <https://learn.microsoft.com/javascript/api/outlook/office.replyformattachment>

## Tích hợp phía server

Server cần:

1. Render `manifest.xml.template` bằng cách thay toàn bộ `__ASSET_HUB_ORIGIN__` bằng public
   origin HTTPS đã canonicalize (không có dấu `/` cuối) và cho tải dưới tên
   `asset-compensation-hub-outlook.xml`.
2. Phục vụ `taskpane.html`, `taskpane.css`, `taskpane.js`, `logo.png` dưới
   `/outlook-addin/`. Chỉ các static asset và manifest là public; API companion vẫn phải xác thực.
3. CSP cho task pane tối thiểu cho phép script từ
   `https://appsforoffice.microsoft.com`; không có inline script/style. Không gửi
   `X-Frame-Options: DENY` cho các asset task pane.
4. Đóng gói các file trong wheel/container (ví dụ thêm
   `integrations/outlook_addin/*` vào `tool.setuptools.package-data`).

API contract:

- `POST /api/companion/exchange` JSON `{ "code": "..." }`.
- `POST /api/companion/client/emails`, bearer auth, multipart `file`, header
  `X-Asset-Hub-Upload: companion-email-v1`.
- `GET /api/companion/client/draft-packages`, bearer auth.
- `GET /api/companion/client/draft-packages/<32hex>`, bearer auth.
- `POST /api/companion/client/draft-packages/<32hex>/ack`, bearer auth, body `{}`, header
  `X-Asset-Hub-Action: companion-ack-v1`.

Token, role/expiry và item-to-source mapping chỉ được lưu trong `sessionStorage`. Không đưa token
vào URL, query string, `localStorage` hoặc log.

## Kiểm tra

```powershell
pytest -q tests/test_outlook_addin_assets.py
node --check src/asset_compensation/integrations/outlook_addin/taskpane.js
```
