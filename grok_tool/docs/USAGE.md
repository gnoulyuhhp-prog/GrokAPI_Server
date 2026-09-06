# Hướng dẫn sử dụng

Tài liệu này đi từ lần clone đầu đến job chạy ổn. Bản rút gọn nằm ở [README gốc](../../README.md).

## 1. Chuẩn bị

| Thứ | Bắt buộc? | Ghi chú |
|-----|-----------|---------|
| Python 3.11+ | Có | `python --version` |
| Chrome | Chỉ backend `browser` | pydoll điều khiển Chrome thật |
| Sub2API đang chạy | Nếu muốn import | Mặc định `http://localhost:8080` |
| Turnstile solver | Backend `protocol` / `auto` | `CHAY_SOLVER.bat` → `:5072` |
| Hotmail list | Chỉ khi chọn mail `1` | `data/hotmails.txt` — 1 dòng = tối đa 5 Grok (`user`, `user+1@` … `+4`) |

## 2. Cài lần đầu (Windows)

```bat
cd grok_tool
python -m venv venv
venv\Scripts\pip install -r requirements.txt
copy config.example.json config.json
notepad config.json
```

Sửa tối thiểu:

1. `fixed_password` — mật khẩu Grok dùng chung (hoặc để tool random nếu bạn đổi logic).
2. `sub2api.sub2api_url` / `sub2api_user` / `sub2api_pass`.
3. Giữ `name_prefix` = `grok free`, `group` = `grok free` nếu muốn đúng convention hiện tại.

**Đừng** dán token / sheet ID / mật khẩu vào README hay commit.

Linux / WSL:

```bash
cd grok_tool
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp config.example.json config.json
```

## 3. Chọn cách chạy

### A. Web — giống dashboard

```bat
CHAY_WEB.bat
```

Trình duyệt: <http://127.0.0.1:8787>

Luồng điển hình:

1. (Nếu HTTP) bật `CHAY_SOLVER.bat` trước.
2. Loại email: **Temp SMART**.
3. Số lượng: `1` để test, `0` để chạy tới khi Stop.
4. Cách reg: **HTTP ẩn** (nhanh) hoặc **Chrome ẩn**.
5. Bật Auto Sub2API.
6. Start. Xem **Logs** / **Kết quả**.

Đóng CMD web = tắt server.

Chạy nền (tự restart nếu crash):

```bat
venv\Scripts\python.exe -m web_console.daemon
venv\Scripts\python.exe -m web_console.daemon --status
```

### B. CLI — một lệnh

```bat
:: Test 1 acc, HTTP
venv\Scripts\python.exe main.py 0 --count 1 --backend protocol

:: Test 1 acc, Chrome ẩn
venv\Scripts\python.exe main.py 0 --count 1 --backend browser

:: 10 acc Hotmail
venv\Scripts\python.exe main.py 1 --count 10 --backend auto

:: Chạy không dừng
venv\Scripts\python.exe main.py 0 --count 0 --backend protocol
```

Tham số positional:

| Input | Provider |
|-------|----------|
| `0` / Enter | Temp smart — azpop ↔ tmail, tự đổi khi lag |
| `1` | Hotmail (`data/hotmails.txt`) |
| `2` | Chỉ azpopmail.com |
| `3` | Chỉ tmail.wibucrypto.pro |

`--provider hotmail` cũng được (không cần số).

**Hotmail plus-alias:** mỗi dòng trong `hotmails.txt` dùng được tối đa `hotmail_max_aliases` lần (mặc định **5**):

```text
user@hotmail.com        → Grok 1
user+1@hotmail.com      → Grok 2
user+2@hotmail.com      → Grok 3
user+3@hotmail.com      → Grok 4
user+4@hotmail.com      → Grok 5
```

OTP vẫn về inbox gốc (Graph / IMAP). Hết 5 slot mới chuyển dòng sang `hotmails_used.txt`. Ledger: `data/hotmail_aliases.json`. Fail / rate-limit **không** đốt alias. `hotmail_max_aliases: 1` = hành vi cũ (1 dòng = 1 Grok).

### C. Menu terminal

```bat
CHAY_REG.bat
```

Menu màu, chọn provider / số lượng trong cửa sổ. `ESC` vẫn dừng job đang chạy.

### D. `start.bat`

Tạo venv nếu thiếu, cài dependency, hỏi `1` Hotmail / `2` temp, rồi gọi `main.py`. Dùng khi máy mới.

## 4. Solver Turnstile

Backend **protocol** gọi HTTP tới `http://127.0.0.1:5072`. Nếu solver tắt, job fail gần như ngay (timeout / connection refused).

```bat
CHAY_SOLVER.bat
```

Lần đầu:

- Cài `camoufox[geoip]`, `quart`, `patchright`, `rich`
- `python -m camoufox fetch` (tải browser headless)

Giữ cửa sổ solver mở song song với web/CLI. Solver chạy **headless**, không cướp focus.

## 5. Ẩn Chrome / không cướp màn hình

- Web: tick **Ẩn Chrome** → set `GROK_NO_FOCUS=1`.
- Config: `"chrome_window_mode": "lygaz"` (cửa sổ ra ngoài màn).
- Protocol: không mở Chrome signup; chỉ solver headless.

Nếu vẫn thấy cửa sổ: thường là solver đang fetch lần đầu, hoặc backend đang là `browser`.

## 6. Sub2API

Sau khi có cookie SSO, tool gọi API admin `sso-to-oauth` (không bắt buộc mở UI OAuth).

Tên token / slot:

```text
grok free 001
grok free 002
...
```

Group: `grok free` (`sub2api.group`). Số bắt đầu: `sub2api.start_number`.

Nếu import fail, status ledger có thể là `success` + hàng đợi retry (`delivery_queue.json`, local).

Tắt import một job web: bỏ tick **Auto Sub2API** (`GROK_SUB2API=0`).

Muốn gọi chat thì vào Sub2API lấy token user (令牌 / API Keys), đừng lấy pass admin trong config. Viết ở [VSCODE.md](VSCODE.md).

Import lỗi (`success_sub2api…`) thì mở lại Sub2API rồi:

```bat
venv\Scripts\python.exe -m grokreg.tools.continue_sub2api
```

## 7. Google Sheet (tùy chọn)

Mặc định **tắt**. Chỉ bật khi bạn tự deploy Apps Script và điền `webapp_url` / `spreadsheet_id` vào **config local**.

Script mẫu: `scripts/gsheets/` — không chứa ID sheet thật.

## 8. Đọc kết quả

File: `data/accounts.txt` (gitignored)

```text
user@temp.example|••••••••|added_sub2api:grok free 003
user2@temp.example|••••••••|otp_timeout
```

Cột: `email | password | status`.

Web tab **Kết quả** parse file này. Không screenshot file này lên GitHub.

## 9. Dừng an toàn

Bất kỳ lúc nào:

1. `ESC` trong process `main.py`
2. `Ctrl+C`
3. Nút Stop trên web (ghi `data/STOP`)
4. Tự tạo `data/STOP` bằng tay

Job giữa chừng sẽ thoát, không để treo vô hạn nếu listener ESC còn sống.

## 10. Chạy ổn định hơn

- Test **1 acc** trước khi `--count 10`.
- Protocol: solver phải xanh trước.
- Giữa hai acc thành công có cooldown `inter_success_delay_min/max` (mặc định ~45–90s) — đừng set 0 trừ khi bạn hiểu rủi ro rate-limit.
- `rate_limit_cooldown_min` khi dính limit.
- Đừng mở nhiều `main.py --count 10` chồng lên nhau trừ khi cố ý (tool có `kill_old` khi start).

## 11. Checklist “chạy mượt”

- [ ] `config.json` tồn tại, không bị commit
- [ ] Sub2API ping được (nếu bật)
- [ ] Solver `:5072` nếu dùng protocol
- [ ] Test ` --count 1` ra `added_sub2api:` hoặc `success`
- [ ] Web chỉ listen localhost
- [ ] `ESC` thử một lần để chắc stop hoạt động
