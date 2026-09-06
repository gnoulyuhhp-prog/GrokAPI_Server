# Đẩy code lên GitHub mà không lộ thông tin cá nhân

Hướng dẫn dùng tool: [README gốc](../README.md) · [docs/USAGE.md](docs/USAGE.md).

## Nguyên tắc

| Lên GitHub (OK) | Chỉ máy bạn (CẤM push) |
|-----------------|-------------------------|
| Source `.py`, web UI, bat | `config.json` (mk, token, sheet) |
| `config.example.json` | `data/accounts.txt`, `hotmails.txt` |
| `requirements.txt`, README | Chrome profile, cookies, SSO |
| `.gitignore`, scripts | `gsheets_service_account.json`, `.env` |

## Lần đầu setup repo an toàn

```bat
cd /d D:\grok_tool\grok_tool

:: 1) Copy config mẫu (nếu máy mới)
copy config.example.json config.json
:: rồi sửa config.json local — file này đã bị .gitignore

:: 2) Kiểm tra không có secret
venv\Scripts\python.exe scripts\check_no_secrets.py

:: 3) Git
git init
git add .
git status
:: XEM KỸ: không được có config.json / data/*.txt / chrome_profile*

venv\Scripts\python.exe scripts\check_no_secrets.py --staged

git commit -m "Initial public-safe commit"
```

Tạo repo trên GitHub (**Private** khuyến nghị), rồi:

```bat
git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git
git branch -M main
git push -u origin main
```

## Trước mỗi lần push

```bat
venv\Scripts\python.exe scripts\check_no_secrets.py --staged
git status
git push
```

Nếu script báo `UNSAFE` → **đừng push**, xóa file/secret khỏi staging:

```bat
git reset HEAD config.json
git rm --cached config.json
```

## Checklist nhanh

- [ ] Không có `config.json` trong `git status`
- [ ] Không có `data/accounts.txt` / `hotmails.txt`
- [ ] Không có file `*service_account*.json`
- [ ] README không dán email / pass / URL sheet / token thật
- [ ] Screenshot không lộ acc/pass
- [ ] Repo **Private** nếu còn nghi ngờ

## Nếu đã lỡ push secret

1. **Đổi ngay** password / API key / webapp secret / rotate sheet permission  
2. Xóa file khỏi history (filter-repo / BFG) hoặc xóa repo + tạo lại  
3. Coi secret đó là **đã lộ** — không chỉ “xóa commit mới”

## File liên quan

- `.gitignore` — chặn data & config  
- `config.example.json` — template không secret  
- `scripts/check_no_secrets.py` — quét trước push  
- `data/README.md` — giải thích folder local  
