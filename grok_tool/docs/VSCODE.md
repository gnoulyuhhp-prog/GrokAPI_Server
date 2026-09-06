# Chạy tool trong VS Code, lấy key Sub2API

Mở folder `grok_tool` trong VS Code (cái có `main.py`), bấm `` Ctrl+` `` ra terminal. Đứng đúng thư mục đó.

## Cài một lần

```powershell
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt
copy config.example.json config.json
```

Sửa `config.json` trên máy mình, đừng commit file này:

- `fixed_password` — pass đặt cho acc Grok mới
- `sub2api_url` — thường `http://127.0.0.1:8080`
- `sub2api_user` / `sub2api_pass` — acc **admin** Sub2API, để tool nhét SSO vào
- `name_prefix` và `group` để `grok free` nếu đang dùng convention đó

WSL thì `python3 -m venv venv` rồi `source venv/bin/activate`. Interpreter VS Code: `Ctrl+Shift+P` → Python: Select Interpreter → chọn `venv`.

## Chạy

Cần Sub2API đang mở (`:8080`). Protocol thì thêm solver.

Terminal 1:

```powershell
.\venv\Scripts\python.exe -m services.turnstile_solver.start
```

Chờ `http://127.0.0.1:5072` lên.

Terminal 2:

```powershell
# temp mail, 1 acc
.\venv\Scripts\python.exe main.py 0 --count 1 --backend protocol

# hotmail (1 dòng hotmails.txt tối đa 5 Grok: mail / +1 … +4)
.\venv\Scripts\python.exe main.py 1 --count 5 --backend protocol

# hoặc web
.\venv\Scripts\python.exe -m web_console.app
```

Web nằm ở http://127.0.0.1:8787/#/register. Chọn Hotmail thì dán list hoặc Browse, Start tự lấy số slot. Tick Auto Sub2API nếu muốn import luôn.

Xong thì `data/accounts.txt` có dòng `added_sub2api:grok free 0xx`. Muốn dừng thì ESC hoặc nút Stop.

## Lấy API key

Hai thứ khác nhau:

- User/pass trong `config.json` = admin, chỉ để **nhét acc** vào Sub2API
- Key dùng curl / Grok Build / Cursor = token trên giao diện Sub2API

Vào http://127.0.0.1:8080, login user (không phải nhét admin vào header Bearer). Menu thường gọi **令牌**, Tokens hoặc API Keys — tùy bản. Tạo token mới, group chọn `grok free` nếu nó hỏi. Copy key, giữ ở máy, đừng đẩy git.

Acc `grok free 001`, `002`… là slot tool vừa import. Token chỉ đi qua pool đó.

## Gọi thử trong terminal

Base hay gặp: `http://127.0.0.1:8080/v1`

```powershell
$env:SUB2API_KEY = "dán key vào đây"

curl.exe http://127.0.0.1:8080/v1/models -H "Authorization: Bearer $env:SUB2API_KEY"

curl.exe http://127.0.0.1:8080/v1/chat/completions `
  -H "Authorization: Bearer $env:SUB2API_KEY" `
  -H "Content-Type: application/json" `
  -d "{\"model\":\"grok-4.5\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}"
```

Tên model lấy trên UI Sub2API, đừng cứng `grok-4.5` nếu bản bạn khác.

Linux:

```bash
export SUB2API_KEY='dán key vào đây'
curl http://127.0.0.1:8080/v1/models -H "Authorization: Bearer $SUB2API_KEY"
```

Grok Build thì `base_url=http://127.0.0.1:8080/v1`, trỏ key qua `env_key=SUB2API_KEY`. Đừng ghi đè `OPENAI_API_KEY` của máy nếu còn dùng OpenAI thiệt.

## Reg được mà Sub2API không nhận

Dòng status là `success` hoặc `success_sub2api…` nghĩa là acc đã có, bước import fail. Mở lại Sub2API, check admin, rồi:

```powershell
.\venv\Scripts\python.exe -m grokreg.tools.continue_sub2api
```

401 là đang nhét nhầm pass admin vào Bearer, hoặc token chết — tạo lại trên UI. 404 `/v1/models` thì thử `/api/v1/models`. Chat không ra thì acc chưa `added_sub2api` hoặc token sai group. Protocol treo thì nhìn lại tab solver.
