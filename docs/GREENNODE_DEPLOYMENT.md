# Triển khai GreenNode vServer

Phương án khuyến nghị cho bản hackathon là một GreenNode vServer chạy Docker
Compose. Caddy nhận lưu lượng `80/443`, cấp TLS và chuyển tiếp tới ứng dụng ở
mạng nội bộ Docker. SQLite và output nằm tại `/var/data`; không chạy nhiều hơn
một app container khi chưa chuyển sang PostgreSQL và object storage.

## Cấu hình hạ tầng đề xuất

- Region/zone: HCM, zone phổ thông đang khả dụng.
- Image: Ubuntu 22.04 LTS x64 UEFI.
- Flavor tối thiểu: general purpose, 1 vCPU và 2 GB RAM; tăng lên 2 vCPU/4 GB
  nếu nhiều người đồng thời build hoặc export workbook.
- Root SSD: 20 GB cho demo; dùng volume mã hoá riêng nếu có dữ liệu vận hành.
- Một Floating IP.
- Security Group: mở `80/tcp`, `443/tcp`, `443/udp`; chỉ mở SSH từ IP quản trị.
- Một VPC/subnet riêng cho product và một SSH public key quản trị.

Trước khi bấm **Launch Server**, đọc lại tổng tiền trên wizard GreenNode. Việc
tạo server, Floating IP, volume và backup có thể phát sinh phí. Không dùng GPU
hay VKS cho kiến trúc SQLite một instance này.

## Chuẩn bị tài khoản GreenNode

1. Đảm bảo tài khoản có đủ credit và quota vServer.
2. Tạo VPC riêng, ví dụ `asset-hub-vpc` với CIDR `10.42.0.0/16`.
3. Tạo subnet `asset-hub-subnet`, CIDR `10.42.1.0/24`, trong zone đã chọn.
4. Import public key SSH chuyên dùng cho deploy. Không tải private key lên
   GreenNode, GitHub, Codex hoặc repository.
5. Tạo Security Group chỉ cho phép HTTP/HTTPS công khai và SSH từ IP quản trị.
6. Tạo vServer, gắn Floating IP, rồi trỏ bản ghi DNS `A` của domain demo tới IP.

Caddy chỉ tự cấp chứng chỉ công khai khi domain đã phân giải tới Floating IP và
cổng `80/443` truy cập được. Không chia sẻ Basic Auth qua HTTP thuần.

## Chuẩn bị Ubuntu

Kết nối bằng SSH key và port đã cấu hình trong Security Group, sau đó cài Git,
Docker Engine và Docker Compose plugin theo tài liệu chính thức của Docker.
Xác nhận:

```bash
git --version
docker --version
docker compose version
```

Tạo thư mục dữ liệu cho UID `1000` mà image ứng dụng sử dụng:

```bash
sudo install -d -o 1000 -g 1000 -m 750 /srv/asset-hub/data
sudo install -d -o 1000 -g 1000 -m 750 /srv/asset-hub/data/reference
```

Clone bản release đã review:

```bash
git clone https://github.com/AnhLNQ2202/DEN-BU-HU-HONG-THIET-BI.git
cd DEN-BU-HU-HONG-THIET-BI
git switch main
git pull --ff-only
```

## Cấu hình secrets

```bash
cp .env.greennode.example .env.greennode
chmod 600 .env.greennode
openssl rand -hex 32
openssl rand -base64 32
```

Đưa hai giá trị ngẫu nhiên vào `ASSET_HUB_SECRET_KEY` và
`ASSET_HUB_ACCESS_PASSWORD`, rồi đặt domain thật tại
`ASSET_HUB_SITE_ADDRESS`. Không commit `.env.greennode`.

Giữ `ASSET_HUB_DEMO_MODE=true` cho staging/hackathon. Trước khi dùng dữ liệu
thật, đổi sang `false`, xoá database demo hoặc dùng data directory mới, và đặt
template/supplier đã phê duyệt dưới `/srv/asset-hub/data/reference`.

## Deploy và smoke test

```bash
docker compose \
  --env-file .env.greennode \
  -f compose.greennode.yaml \
  config --quiet

docker compose \
  --env-file .env.greennode \
  -f compose.greennode.yaml \
  up -d --build

docker compose \
  --env-file .env.greennode \
  -f compose.greennode.yaml \
  ps
```

Kiểm tra:

```bash
curl --fail --silent --show-error https://asset-hub.example.com/api/health
curl --head https://asset-hub.example.com/
curl --user judge https://asset-hub.example.com/api/dashboard
```

Lệnh thứ hai phải trả `401`; lệnh thứ ba sẽ hỏi password và phải trả JSON sau
khi đăng nhập đúng. Tiếp tục kiểm tra dashboard, tính thử TranNNB, tạo batch
demo, tải workbook, restart container và xác nhận dữ liệu vẫn còn.

```bash
docker compose --env-file .env.greennode -f compose.greennode.yaml restart app
docker compose --env-file .env.greennode -f compose.greennode.yaml logs --tail 200
```

## Cập nhật và rollback

Trước mỗi deploy, ghi lại commit đang chạy và tạo SQLite backup nhất quán. Với
demo ít người dùng, có thể dừng app ngắn để sao lưu:

```bash
docker compose --env-file .env.greennode -f compose.greennode.yaml stop app
sudo cp -a /srv/asset-hub/data /srv/asset-hub/data-backup-before-deploy
docker compose --env-file .env.greennode -f compose.greennode.yaml start app
```

Cập nhật code bằng commit/branch đã qua CI rồi chạy lại `up -d --build`. Nếu
smoke test lỗi, checkout commit trước và build lại. Rollback code không rollback
SQLite; chỉ phục hồi data backup khi đã dừng app và xác nhận đó là quyết định
mong muốn.

Với dữ liệu quan trọng, dùng volume mã hoá riêng và GreenNode Snapshot/vBackup.
Snapshot block trong lúc SQLite đang ghi có thể không nhất quán, vì vậy hãy dừng
app hoặc tạo SQLite backup trước snapshot.

## Tuỳ chọn vContainer Registry

Khi cần rollback nhanh và không muốn build trên vServer, build image
`linux/amd64`, tag bằng Git SHA, push vào private vCR, rồi đặt
`ASSET_HUB_IMAGE` thành tag đó. Repository user trên vServer chỉ cần quyền Pull
Only. Giữ ít nhất hai hoặc ba image SHA gần nhất; không dùng duy nhất tag
`latest` cho rollback.

## Rollback triggers

- `/api/health` không trả `200` sau 3 lần kiểm tra liên tiếp.
- Dashboard, đăng nhập hoặc tải bundle React thất bại.
- Tính preview hoặc xuất workbook demo thất bại.
- Container restart lặp lại, log có exception liên tục hoặc volume mất quyền ghi.
- Dữ liệu không còn sau restart.

Khi một trigger xảy ra, ngừng ingest/export, giữ nguyên data volume, quay về
image/commit trước và chạy lại toàn bộ smoke test.

## Tài liệu chính thức

- [Tạo GreenNode vServer](https://docs.vngcloud.vn/vng-cloud-document/vserver/compute-hcm03-1a/instance/create-an-instance-by-using-the-wizard)
- [Floating IP](https://docs.vngcloud.vn/vng-cloud-document/vserver/compute-hcm03-1a/vpc/floating-ip)
- [vContainer Registry](https://docs.vngcloud.vn/vng-cloud-document/vcontainer-registry/getting-started)
- [GreenNode Snapshot](https://docs.vngcloud.vn/vng-cloud-document/vserver/compute-hcm03-1a/snapshot/create-snapshots)
- [Cài Docker Engine trên Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
