# Môi trường phát triển và kiểm thử cho team

Mỗi thành viên có thể clone repository, sửa code bằng Codex hoặc Claude Code
trên máy riêng và chạy cùng một môi trường demo. Cách khuyến nghị là để agent
chạy trên máy host, còn React/Flask chạy bằng Docker Compose với hot reload.
Không cần và không được dùng email, workbook hay dữ liệu nhân sự thật.

## Khởi động trong dưới 5 phút

Yêu cầu: Git và Docker Desktop (Windows/macOS) hoặc Docker Engine kèm Compose
plugin (Linux).

```bash
git clone https://github.com/AnhLNQ2202/DEN-BU-HU-HONG-THIET-BI.git
cd DEN-BU-HU-HONG-THIET-BI
docker compose -f compose.dev.yaml up --build
```

Mở <http://localhost:5173>. React chạy hot reload; API Flask ở
<http://localhost:5000/api/health>. Dữ liệu được tạo tự động là dữ liệu demo
synthetic và nằm trong Docker volume, không nằm trong Git. Hai cổng chỉ bind vào
`127.0.0.1`, vì vậy máy khác trong LAN không truy cập được môi trường local.

Dừng môi trường bằng `Ctrl+C`, sau đó:

```bash
docker compose -f compose.dev.yaml down
```

Chỉ khi muốn xoá toàn bộ database demo local và cài lại volume dependencies:

```bash
docker compose -f compose.dev.yaml down -v
```

Lệnh có `-v` xoá dữ liệu demo trong volume của môi trường dev và không thể hoàn
tác. Nó không chạm vào Render hay dữ liệu của thành viên khác.

## Hai cách làm việc

### Cách A — Agent trên host, ứng dụng trong Docker (khuyến nghị)

1. Chạy Compose như phần quick start.
2. Mở đúng thư mục clone bằng Codex Desktop/CLI hoặc Claude Code.
3. Giao một task nhỏ, nêu tiêu chí hoàn tất và yêu cầu agent chạy kiểm thử.
4. Xem thay đổi ngay tại cổng `5173`; Flask và Vite tự reload khi file đổi.
5. Kiểm tra `git diff`, chạy quality checks rồi mới commit.

Cách này hoạt động giống nhau trên Windows, macOS và Linux. Token đăng nhập của
Codex/Claude chỉ nằm trên host; không mount token vào container.

### Cách B — Development Container

Repository có `.devcontainer/devcontainer.json` cho VS Code Dev Containers và
GitHub Codespaces. Chọn **Reopen in Container**. Lần đầu container sẽ cài Python,
pnpm và seed dữ liệu demo. Sau đó mở hai terminal trong container:

```bash
python -m flask --app asset_compensation.web.app:create_app run \
  --debug --host 0.0.0.0 --port 5000
```

```bash
pnpm --dir frontend dev
```

Cổng `5000` và `5173` được forward sẵn. Dev container hữu ích khi máy thành
viên có phiên bản Python/Node khác nhau. Nó không chứa Codex/Claude credentials;
mỗi người vẫn phải đăng nhập bằng tài khoản của mình.

Tài liệu chính thức: [VS Code Dev Containers](https://code.visualstudio.com/docs/devcontainers/create-dev-container)
và [GitHub Codespaces dev containers](https://docs.github.com/en/codespaces/setting-up-your-project-for-codespaces/adding-a-dev-container-configuration/introduction-to-dev-containers).

## Dùng Codex và Claude Code

### Codex

- Codex Desktop: chọn repository đã clone làm project rồi tạo task mới.
- Codex CLI: mở terminal tại root repository và chạy `codex`.
- Bắt đầu task bằng việc yêu cầu đọc `AGENTS.md` và
  `docs/TEAM_DEVELOPMENT.md`; file `AGENTS.md` là quy tắc chung của repo.
- Chỉ cấp quyền cho lệnh cần thiết và luôn review `git diff` trước commit.

Xem [hướng dẫn Codex CLI](https://help.openai.com/en/articles/11096431) và
[đăng nhập Codex bằng ChatGPT](https://help.openai.com/en/articles/11381614-api-codex-cli-and-sign-in-with-chatgpt).

### Claude Code

- Mở terminal tại root repository và chạy `claude`.
- `CLAUDE.md` dẫn Claude tới cùng quy tắc trong `AGENTS.md`, nên hai agent dùng
  cùng ràng buộc về UI, template, dữ liệu và kiểm thử.
- Trên Windows, làm theo lựa chọn WSL hoặc Git Bash trong tài liệu chính thức.

Xem [Claude Code setup](https://docs.anthropic.com/en/docs/claude-code/getting-started).

Không để Codex và Claude cùng sửa một worktree tại cùng thời điểm. Nếu cần chạy
song song, tạo hai worktree/branch độc lập:

```bash
git fetch origin
git worktree add ../asset-hub-ui -b feat/ui origin/main
git worktree add ../asset-hub-api -b feat/api origin/main
```

Mỗi agent chỉ mở đúng worktree được giao. Ghép thay đổi bằng pull request hoặc
cherry-pick commit đã review, không copy file thủ công giữa hai thư mục.

## Quy trình branch và pull request

Không sửa trực tiếp `main`:

```bash
git switch main
git pull --ff-only
git switch -c feat/ten-tinh-nang-ngan
```

Quy ước branch gợi ý: `feat/...`, `fix/...`, `docs/...`, `refactor/...`.

Trước khi push:

```bash
git status --short
git diff --check
docker compose -f compose.dev.yaml exec backend ruff check .
docker compose -f compose.dev.yaml exec backend \
  pytest --cov=asset_compensation --cov-report=term-missing
docker compose -f compose.dev.yaml exec frontend pnpm build
```

`pnpm build` tạo lại bundle tại
`src/asset_compensation/web/static/dist`; không sửa các file bundle bằng tay.
Commit source và bundle tương ứng trong cùng pull request nếu bundle thay đổi.

Sau đó push branch và mở PR. GitHub Actions chạy lại frontend build, lint,
backend tests và production-container build. Chỉ merge khi checks xanh và có ít
nhất một thành viên khác review những thay đổi nghiệp vụ hoặc template.

## Test theo phạm vi

| Loại thay đổi | Kiểm tra tối thiểu |
| --- | --- |
| React/UI | `pnpm build`, mở `5173`, đi qua flow đã đổi, so với UI gốc |
| Flask/API/service | `ruff check .`, `pytest --cov=asset_compensation` |
| Parser/Excel | Test bằng fixture synthetic; kiểm tra cảnh báo và workbook output |
| Docker/deploy | `docker build -t asset-compensation-hub:local .`, gọi `/api/health` |
| Tài liệu/config | Kiểm tra lệnh, link, YAML/JSON và chạy quick start nếu có Docker |

Container Linux không chạy các adapter COM của Outlook/Word. Core tests không
phụ thuộc Office. Nếu một task thực sự đụng COM/VBA, kiểm thử bổ sung trên máy
Windows có Office được IT phê duyệt, với dữ liệu giả; không đưa file đó vào Git.

## Dữ liệu, secrets và file output

- `.env.example` chỉ chứa giá trị minh hoạ an toàn. Ứng dụng không tự động load
  file `.env`; Compose/dev container đã truyền các biến dev cần thiết.
- Không commit `.env`, token GitHub, token Codex/Claude, mật khẩu Render, database,
  EML/MSG/PDF, file Office thật, supplier export hoặc dữ liệu nhân viên.
- Không dán dữ liệu thật vào prompt của coding agent. Dùng fixture synthetic đã
  ẩn danh và nhỏ nhất có thể.
- Dữ liệu runtime local thuộc `var/` hoặc Docker volume. Template vận hành và
  file tham chiếu thật phải nằm ngoài repository.
- Trước PR, chạy `git status --short` và kiểm tra file lạ; `.gitignore` chỉ là
  lớp bảo vệ, không thay thế việc review.

## Preview cloud tách khỏi production

`render.yaml` và `Dockerfile` hiện tại là đường production. Không đổi nhánh
production sang branch thử nghiệm và không dùng chung persistent disk.

Khi team cần một URL để cùng test PR:

1. Tạo một Render Web Service **riêng** trỏ vào branch integration/PR.
2. Dùng cùng `Dockerfile`, bật `ASSET_HUB_DEMO_MODE=true` và đặt
   `ASSET_HUB_DATA_DIR=/tmp/asset-hub-preview` nếu preview không có disk.
3. Tạo username/password Basic Auth riêng cho preview; không sao chép secret
   production.
4. Chỉ dùng dữ liệu synthetic. Preview không có disk sẽ mất dữ liệu sau restart,
   đây là hành vi mong đợi.
5. Ghi URL preview vào PR, test, rồi xoá service preview khi merge/đóng PR.

Nếu cần giữ trạng thái qua restart, cấp một disk riêng cho preview; tuyệt đối
không mount disk production. Cloud preview dùng để review/test, còn chỉnh code
vẫn thực hiện qua clone/worktree rồi push branch.

## Xử lý lỗi thường gặp

- Cổng bận: dừng tiến trình đang dùng `5000`/`5173` hoặc đổi port mapping ở bản
  copy local của `compose.dev.yaml`; không commit thay đổi cá nhân đó.
- Frontend báo API lỗi: kiểm tra
  `docker compose -f compose.dev.yaml ps` và mở `/api/health`.
- Dependency thay đổi nhưng container chưa nhận: chạy lại
  `docker compose -f compose.dev.yaml up --build`.
- Cần demo sạch: dùng `down -v`, sau đó `up --build`; thao tác này chỉ xoá volume
  local của project.
- Dev container setup lỗi: chạy lại `.devcontainer/post-create.sh` bên trong
  container rồi kiểm tra Python/pnpm trước khi cài thêm công cụ khác.
