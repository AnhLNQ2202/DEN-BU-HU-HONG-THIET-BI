# Render Free staging

`render.staging.yaml` tạo một Web Service riêng cho việc review và kiểm thử
hackathon. Blueprint này không thay đổi `render.yaml` dùng cho đường triển khai
có persistent disk.

## Tạo staging

1. Trong Render Dashboard, chọn **New > Blueprint** và kết nối repository.
2. Chọn nhánh cần test và đặt **Blueprint Path** là
   `render.staging.yaml` thay vì đường dẫn mặc định `render.yaml`.
3. Khi Render hỏi `ASSET_HUB_ACCESS_PASSWORD`, nhập một mật khẩu staging mạnh,
   riêng biệt. Không commit hoặc gửi mật khẩu vào issue/PR.
4. Kiểm tra `plan` là **Free**, service không có disk/database trả phí rồi mới
   Apply.
5. Sau khi deploy, mở `/api/health`, đăng nhập dashboard bằng user `judge`, sau
   đó thực hiện smoke test bên dưới.

Không tạo thủ công biến `PORT`. `Dockerfile` đã chạy một Gunicorn worker và
bind vào `0.0.0.0:${PORT:-10000}`; Blueprint chỉ giảm số thread xuống `2` cho
instance Free.

## Dữ liệu dùng một lần

- SQLite, inbox và output nằm tại `/tmp/asset-hub-staging`.
- `ASSET_HUB_DEMO_MODE=false` giữ dashboard trống sau deploy. Dữ liệu test chỉ
  xuất hiện khi người dùng chủ động nạp fixture synthetic.
- `ASSET_HUB_ALLOW_TEST_RESET=true` chỉ mở nút **Xóa dữ liệu test** trên staging
  dùng một lần. Nút yêu cầu xác nhận rồi xoá case, batch, file output và bản
  Supplier đã upload; không đụng tới inbox hay template cấu hình bên ngoài.
- Render Free có filesystem tạm: restart, redeploy hoặc spin-down có thể xoá
  mọi thay đổi và file output. Đây là hành vi mong đợi của staging.
- Chỉ upload cặp Supplier và EML synthetic/đã ẩn danh để test tính
  năng. Không dùng email, Supplier, PDF, workbook hay thông tin nhân viên
  vận hành thật trên staging công khai này.

Nếu cần giữ dữ liệu, không gắn disk vào service Free này. Hãy dùng blueprint
trả phí `render.yaml` hoặc thiết kế storage/database riêng sau khi review yêu
cầu bảo mật.

## Smoke test

- `GET /api/health` trả `200`, `"ok": true` và `"demo_mode": false`. Endpoint
  này cố ý không yêu cầu Basic Auth để Render health check hoạt động.
- `/` trả `401` khi chưa đăng nhập và tải dashboard sau khi dùng tài khoản
  staging.
- Dashboard khởi đầu trống. Upload cặp Supplier synthetic, sau đó upload
  EML synthetic; case mới phải xuất hiện mà không cần restart.
- Với case `LOST`, chọn case trong màn TranNNB và kiểm tra chỉ các
  trường thật sự có trong mail được điền sẵn.
- Bấm **Xóa dữ liệu test**, xác nhận, rồi kiểm tra dashboard và trạng thái
  Supplier trở về trống.
- Sau restart/redeploy, dữ liệu có thể bị xoá và dashboard trở lại
  trạng thái trống.
- Log không chứa mật khẩu hoặc nội dung dữ liệu nghiệp vụ.

## Giới hạn và dọn dẹp

Free service có thể ngủ khi không có traffic và lần mở tiếp theo có thể phải
chờ cold start. Blueprint dùng `checksPass`, vì vậy chỉ tự deploy khi CI của
nhánh đã qua. Xoá service/Blueprint trong Render khi team không còn cần URL
staging để tránh nhầm với production.

Tài liệu chính thức:

- [Render Free](https://render.com/docs/free)
- [Blueprint YAML](https://render.com/docs/blueprint-spec)
- [Docker trên Render](https://render.com/docs/docker)
- [Health checks](https://render.com/docs/health-checks)
