# Triển khai Render

Cấu hình trong repository triển khai một Render Web Service duy nhất:

1. Docker build giao diện React/Vite.
2. Image Python nhận thư mục `dist` và chạy Flask bằng Gunicorn.
3. Flask phục vụ cả SPA và `/api/*` trên cùng domain.
4. SQLite, file inbox và workbook đầu ra nằm trong persistent disk tại
   `/var/data`.

Cách này tránh CORS và phù hợp bản thi hackathon. Do giới hạn của
SQLite và Render disk, service được cố định **một instance**.

## Chuẩn bị

- Repository đã được push lên GitHub.
- Nhánh triển khai phải qua GitHub Actions; `render.yaml` chỉ tự động deploy
  sau khi checks thành công.
- Tài khoản Render có phương thức thanh toán. Persistent disk không có
  trên Free web service, vì vậy Blueprint dùng plan `starter` và disk 1 GB.

Không commit email, PDF, workbook, database hoặc file `.env` có dữ liệu thật.
`.gitignore` và `.dockerignore` đã chặn các loại file này.

## Tạo service từ Blueprint

1. Trong Render Dashboard, chọn **New > Blueprint**.
2. Kết nối repository và chọn nhánh cần triển khai.
3. Render đọc `render.yaml`. Kiểm tra lại các giá trị trước khi Apply:
   - runtime `docker`;
   - region `singapore`;
   - plan `starter`;
   - một instance;
   - persistent disk `/var/data`, 1 GB.
4. Apply Blueprint và chờ build hoàn tất.
5. Mở URL của service và chạy checklist smoke test bên dưới.

Region của Render service không thể thay đổi sau khi tạo; `singapore` được
chọn để giảm độ trễ cho bản demo tại Việt Nam. Nếu cần region khác,
hãy sửa `render.yaml` trước lần Apply đầu tiên.

## Biến môi trường

Blueprint thiết lập:

| Biến | Mục đích |
| --- | --- |
| `ASSET_HUB_DATA_DIR=/var/data` | Đặt SQLite và output trên persistent disk. |
| `ASSET_HUB_DEMO_MODE=true` | Tự tạo dữ liệu synthetic khi database trống. |
| `ASSET_HUB_SECRET_KEY` | Render tự sinh, không lưu trong Git. |
| `ASSET_HUB_ACCESS_USER=judge` | Tài khoản Basic Auth cho bản demo công khai. |
| `ASSET_HUB_ACCESS_PASSWORD` | Render tự sinh; lấy trong Dashboard để chia sẻ cho giám khảo. |
| `GUNICORN_THREADS=4` | Cho phép nhiều request I/O trên một worker. |
| `GUNICORN_TIMEOUT=120` | Giới hạn thời gian cho thao tác export. |

Các biến FA&GL/CCDC, Tran template, opt-in EML retention, draft sender và policy
GL được liệt kê trong [TRAN_API.md](TRAN_API.md). Không đưa đường dẫn local
Windows hoặc GL vận hành vào Blueprint/commit; trên cloud nên upload reference
synthetic qua UI hoặc mount một private data volume.

Render tự cung cấp `PORT`; Docker command bind Gunicorn vào
`0.0.0.0:$PORT`. Không tạo `PORT` thủ công.

Cho bản demo hackathon, giữ `ASSET_HUB_DEMO_MODE=true`. Trước khi nạp dữ
liệu thật, đổi thành `false` trong Render Dashboard và cấu hình các tài
khoản GL đã được phê duyệt. File supplier thật phải nằm trên disk,
ví dụ `/var/data/reference/suppliers.xlsx`, sau đó đặt
`ASSET_HUB_SUPPLIER_FILE` thành đường dẫn đó.

Workbook demo dùng bản template sạch đi kèm image: một sheet, đúng 30 cột
import ban đầu và không chứa dữ liệu nghiệp vụ. Nếu cần giữ nguyên template
`.xlsm` đã được phê duyệt (kể cả VBA), đặt file tại
`/var/data/reference/Template_DENBU2.xlsm` rồi cấu hình:

- `ASSET_HUB_ACCOUNTING_TEMPLATE=/var/data/reference/Template_DENBU2.xlsm`
- `ASSET_HUB_ACCOUNTING_ORG_ID=<org-id đã được Finance phê duyệt>`

Không đưa template vận hành vào Git hoặc Docker image. Ứng dụng kiểm tra đúng
30 header trước khi ghi, giữ style dòng và giữ nguyên macro payload của `.xlsm`.

Blueprint bật Basic Auth mặc định. Xem giá trị secret
`ASSET_HUB_ACCESS_PASSWORD` trong Render Dashboard và chỉ chia sẻ với người
cần xem demo. Nếu thay username/password, phải cấu hình cả hai; ứng dụng
sẽ từ chối khởi động nếu chỉ có một giá trị.

## Kiểm tra trước khi Apply

Render CLI có thể validate Blueprint mà không deploy:

```powershell
render blueprints validate render.yaml
```

Build và chạy image local:

```powershell
docker build -t asset-compensation-hub .
docker volume create asset-hub-data
docker run --rm -p 10000:10000 `
  -e PORT=10000 `
  -e ASSET_HUB_DATA_DIR=/var/data `
  -e ASSET_HUB_DEMO_MODE=true `
  -v asset-hub-data:/var/data `
  asset-compensation-hub
```

Sau đó mở <http://localhost:10000/api/health>. Dừng container và chạy lại
cùng volume để xác nhận dữ liệu còn nguyên.

## Smoke test sau deploy

- `GET /api/health` trả `200` và `"ok": true`.
- Dashboard yêu cầu Basic Auth, sau đó tải được mà không có lỗi
  asset hay CORS.
- Case demo xuất hiện; thay đổi một case và tải workbook của batch.
- Manual restart service, sau đó xác nhận case và workbook vẫn còn.
- Kiểm tra log không in secret hoặc nội dung file nghiệp vụ.

## Rollback và giới hạn

Rollback một Render deploy chỉ quay lại code/image; nó không quay lại nội
dung persistent disk. Nếu cần phục hồi disk snapshot, hãy dừng ghi dữ liệu
trước, phục hồi snapshot riêng, rồi smoke test lại.

Render disk chỉ gắn vào một service instance và không dùng được trong
build/pre-deploy. Không tăng `numInstances` khi còn dùng SQLite. Trước khi
chuyển sang môi trường nhiều người dùng hoặc multi-instance, hãy chuyển
database sang PostgreSQL và tách object storage cho evidence/output.

## Tài liệu chính thức

- [Deploy Flask và Gunicorn trên Render](https://render.com/docs/deploy-flask)
- [Docker trên Render](https://render.com/docs/docker)
- [Web services và ràng buộc host/port](https://render.com/docs/web-services)
- [Persistent disks](https://render.com/docs/disks)
- [Blueprint specification](https://render.com/docs/blueprint-spec)
- [Infrastructure as Code](https://render.com/docs/infrastructure-as-code)
- [Monorepo support](https://render.com/docs/monorepo-support)
- [Health checks](https://render.com/docs/health-checks)
- [Environment variables and secrets](https://render.com/docs/configure-environment-variables)
- [Render CLI reference](https://render.com/docs/cli-reference)
- [Gunicorn package](https://pypi.org/project/gunicorn/)
