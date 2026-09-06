# Playbook: lỗi → log → học tool đối thủ → nâng cấp

**Đối thủ:** `grok-register-web`  
(đường dẫn zip/local: `…/Telegram Desktop/grok-register-web-master/`)

**Nguyên tắc (từ user):** mỗi khi tool mình lỗi — **không đoán mò**.  
1. Đọc log / `data/network_capture_*.json` / status trong `data/accounts.txt`  
2. Tìm chỗ tương ứng trong code đối thủ (đã chạy mượt)  
3. **Áp dụng / port / nâng cấp** vào `grok_tool`  
4. Chạy lại xác nhận  

## Map lỗi → chỗ đối thủ hay xử lý

| Lỗi / triệu chứng | Xem log | Code đối thủ (ưu tiên) |
|-------------------|---------|-------------------------|
| Cloudflare / Turnstile / iframe fail | `Unable to create isolated world`, CF timeout | `core/registration/turnstile.py`, `services/turnstile_solver/`, `services/solver_manager.py` |
| Complete → `/sign-in`, không session | `signup_ok_session_fail`, `tosPatched=0` | `core/registration/backend.py` (SSO extract), `tosAcceptedVersion` int32 |
| Có cookie `sso`/`sso-rw` nhưng tool vẫn fail | `[sso] captured` + vẫn `session_fail` | Dùng SSO cho delivery, **đừng** bắt UI login; `sub2api_client.import_sso` / `sso-to-oauth` |
| Sub2API fail | OAuth timeout, no SSO | `core/sub2api_client.py` — API `sso-to-oauth` trước, browser OAuth sau |
| OTP / mail | `otp_timeout`, domain slow | `core/mail_providers.py`, `core/email_manager.py` |
| Durable upload | reg OK upload fail | `core/grok2api_retry.py` |
| Protocol reg (ít Chrome) | browser kẹt | `core/registration/protocol_worker.py`, `signup.py` |

## Thứ tự ưu tiên khi port

1. **Không phá** naming `grok free NNN` + group `grok free` + Google Sheet  
2. **SSO cookie → Sub2API API** (đối thủ) > browser OAuth UI  
3. **External Turnstile solver** (`CHAY_SOLVER.bat` :5072) > pydoll click iframe  
4. Reg success **độc lập** upload (durable queue)  
5. Chỉ port protocol worker full khi browser path vẫn fail sau CF+SSO  

## File mình đã học từ đối thủ

- `turnstile_solver_client.py` + `services/turnstile_solver/` + `CHAY_SOLVER.bat`  
- `sub2api_client.py` (sso-to-oauth)  
- `sso_capture.py`, `delivery_retry.py`  
- TOS `tosAcceptedVersion` int patch trong `main.py`  

## Checklist trước khi “fix xong”

- [ ] Log lỗi gốc đã khớp root cause (không chỉ symptom)  
- [ ] Có đoạn code/đối thủ tương ứng (file + ý)  
- [ ] Patch vào tool mình + chạy lại 1 acc  
- [ ] Status ledger: `success` / `added_sub2api:*` (không còn fail oan khi đã có SSO)  
