# GrokAPI Server — VPS source write-up

## Mục tiêu

Đây là source export trực tiếp từ VPS, gồm GrokReg tooling và bản Sub2API
đang được dùng cho gateway. Các file bí mật, dữ liệu live và artifact build
đã được loại khỏi export.

## Thành phần

```text
grok_tool/                  GrokReg CLI, portal, web console, solver
sub2api/backend/            Go API, auth, quota, billing, gateway
sub2api/frontend/           Vue SPA, admin/user views
sub2api/deploy/vps/         Dockerfile, compose và Caddy policy
sub2api/backend/migrations/ Database migrations
```

### GrokReg

`grok_tool/main.py` là entry point cho worker đăng ký. `portal.py` chạy portal
HTTP trên cổng 8082. `services/solver_manager.py` quản lý local Turnstile
solver; solver Camoufox dùng cổng 5072 và có browser pool theo số thread.

Worker là thành phần tùy chọn. Trạng thái vận hành hiện tại trên VPS là
worker 24/7 đã được tắt; portal và Sub2API vẫn chạy độc lập.

### Sub2API

Backend Go phục vụ API tương thích OpenAI, auth, API key, quota/billing,
account pool, usage log và image/chat gateway. Frontend là SPA Vue; backend có
embedded frontend và fallback về `index.html`, nên refresh trực tiếp các route
như `/admin/accounts` không bị 404.

## Luồng request

```text
Client -> Caddy :443/:80
       -> portal :8082 cho /check, /balance và setup scripts
       -> sub2api :8080 cho frontend/API còn lại
sub2api -> PostgreSQL (dữ liệu bền vững)
        -> Redis (cache, rate limit, session)
```

Caddy là public entry point. Backend chỉ publish loopback; Docker healthcheck
đảm bảo Caddy không gửi traffic trước khi backend sẵn sàng.

## Triển khai VPS

Stack nằm ở `sub2api/deploy/vps/compose.yml`, gồm `sub2api`, `sub2api-caddy`,
`sub2api-postgres` và `sub2api-redis`.

```bash
docker compose -f sub2api/deploy/vps/compose.yml ps
curl -fsS http://127.0.0.1:8080/health
docker inspect --format '{{.State.Health.Status}}' \
  sub2api sub2api-postgres sub2api-redis
```

Biến môi trường production phải được nạp từ file secret riêng trên VPS.
Không commit `.env` hoặc credential vào repository.

Portal systemd unit là `grokreg-portal.service`:

```bash
sudo systemctl status grokreg-portal.service
sudo journalctl -u grokreg-portal.service -n 100 --no-pager
```

## Ổn định và watchdog

VPS có watchdog `grok-web-healthcheck.timer` chạy mỗi phút. Watchdog kiểm tra
portal, backend `/health`, SPA route, Caddy, PostgreSQL readiness, Redis health
và dung lượng ổ đĩa. Component lỗi được restart riêng với cooldown chống
restart-loop.

Khi ổ đĩa vượt ngưỡng, watchdog chỉ dọn build cache/profile trình duyệt có
thể tạo lại. Không xóa các volume dữ liệu PostgreSQL, Redis hoặc Sub2API.

Worker nếu được bật phải chạy với giới hạn CPU/RAM/swap và số thread hữu hạn;
không tăng thread vô hạn trên VPS 4 CPU.

## Phát triển local

Python:

```powershell
Set-Location .\grok_tool
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pytest tests -q
```

Frontend:

```powershell
Set-Location .\sub2api\frontend
pnpm install --frozen-lockfile
pnpm build
pnpm test -- --run
```

Backend:

```powershell
Set-Location .\sub2api\backend
go test ./...
```

## Smoke test sau deploy

```powershell
curl.exe -I https://grokapi.vorte.me/
curl.exe -I https://grokapi.vorte.me/login
curl.exe -I https://grokapi.vorte.me/admin/accounts
curl.exe -I https://grokapi.vorte.me/user/keys
curl.exe -I https://grokapi.vorte.me/check
```

Các route giao diện kỳ vọng trả `200`. Login probe với thông tin giả có thể
trả `401 invalid credentials`; đó là dấu hiệu request đã tới auth backend.

## Xử lý sự cố

### Login báo service temporarily unavailable

Kiểm tra ổ đĩa và dependency:

```bash
df -h /
docker inspect --format '{{.State.Health.Status}}' sub2api-postgres
docker logs --tail 120 sub2api-postgres
```

Ổ đĩa đầy có thể làm PostgreSQL từ chối kết nối. Dọn build cache không dùng,
không xóa volume database.

### Route admin/user trả 404

Kiểm tra backend đang phục vụ embedded frontend và Caddy proxy đúng container:

```bash
curl -i http://127.0.0.1:8080/admin/accounts
docker logs --tail 120 sub2api-caddy
```

### Web chậm hoặc VPS swap cao

Kiểm tra `free -h`, `vmstat`, Docker stats và số browser process. Tạm dừng
worker đăng ký trước khi restart backend nếu worker đang chiếm CPU/RAM.

## Bảo mật

- Không commit `.env`, API key, OAuth token, cookie, account list, database
  dump, SSH private key hoặc service-account credential.
- Mỗi thiết bị nên dùng SSH key riêng; tài khoản `root` có toàn quyền.
- Nếu private key từng bị upload nhầm, phải rotate key trên VPS.
- Không ghi password, bearer token hoặc full API key vào log/test fixture.

## Phạm vi export

Export này chứa mã nguồn, migration, test, frontend build cần thiết và cấu hình
mẫu. Không chứa database live, Docker volumes, secret production, cache,
virtualenv, `node_modules`, backup snapshot hay binary deploy lớn.
