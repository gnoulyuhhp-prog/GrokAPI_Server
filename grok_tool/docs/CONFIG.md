# Cấu hình (`config.json`)

Copy từ [`config.example.json`](../config.example.json). File `config.json` **không** được commit.

Mọi giá trị dưới đây là **tên key + ý nghĩa**. Đừng paste secret thật vào docs.

## Mail

| Key | Mặc định mẫu | Ý nghĩa |
|-----|----------------|---------|
| `email_provider` | `auto_temp` | CLI/web ghi đè: `auto_temp`, `hotmail`, `racing`, `tinyhost`, `tempmail_lol`, `tempmail_vip`, `azpopmail`, `tmail_wibu` |
| `temp_mail_order` | `["azpopmail","tmail_wibu"]` | Thứ tự failover temp smart |
| `racing` | `{"tinyhost_base_url": "..."}` | Đua nhiều nguồn temp mail miễn phí cùng lúc |
| `tinyhost` | `{"base_url": "https://tinyhost.shop"}` | Cấu hình TinyHost temp mail |
| `tempmail_lol` | `{"api_key": ""}` | API key TempMail.lol (tùy chọn) |
| `tempmail_vip` | `{"api_key": ""}` | API key TempMailVIP |
| `hotmail_list` | `data/hotmails.txt` | Pool Hotmail (local) |
| `hotmail_max_aliases` | `5` | 1 Hotmail → tối đa N Grok (`mail`, `mail+1@` … `+4`). OTP vẫn về inbox gốc |
| `timeout_otp` | `240` | Giây chờ OTP |
| `azpopmail.*` | URL public | Token để trống nếu không có |
| `tmail_wibu.*` | URL public | Temp provider thứ hai |
| `mail_api.enabled` | `true` | Hotmail Graph / inbox helper |
| `mail_api.client_id` | placeholder | Azure app — để trống hoặc ID của bạn |
| `save_storage_state` | `true` | Tự động lưu full browser session (`data/sessions/<email>.json`) cho Playwright |

## Tài khoản Grok

| Key | Ý nghĩa |
|-----|---------|
| `fixed_password` | Password đặt cho acc mới. Đổi thành chuỗi mạnh **của bạn** |
| `password_length` | Dùng khi random (nếu không fixed) |
| `fixed_first_name` / `fixed_last_name` | Để trống = random |
| `save_file` | Ledger — nên để `data/accounts.txt` |

## Backend & nhịp

| Key | Ý nghĩa |
|-----|---------|
| `reg_backend` | `browser` · `protocol` · `auto` (CLI `--backend` thắng) |
| `batch_count` | Số acc khi không truyền `--count` |
| `inter_success_delay_min` / `_max` | Nghỉ sau acc **thành công** (giây) |
| `rate_limit_cooldown_min` | Nghỉ khi bị rate limit |
| `mail_fail_cooldown_min` | Nghỉ khi mail/OTP fail |
| `humanize` | Gõ / delay giống người (browser) |
| `human_delay_min` / `_max` | Biên delay humanize |

## Chrome (browser backend)

| Key | Ý nghĩa |
|-----|---------|
| `headless` | `false` khuyến nghị — CF/Turnstile thường ghét headless thuần |
| `chrome_user_data_dir` | Profile local, gitignored |
| `chrome_debug_port` | CDP port, mặc định `9333` |
| `fresh_profile_per_account` | Profile mới mỗi acc (chậm hơn, sạch hơn) |
| `reuse_chrome_profile` | Tái sử dụng profile |
| `force_guest_on_start` | Vào khách, giữ `cf_clearance` nếu có |
| `chrome_window_mode` | `lygaz` = đẩy ra ngoài màn |
| `chrome_window_position` | `x,y` nếu không lygaz |
| `chrome_background` | Chạy nền |
| `keep_browser_open` | `false` = đóng sau job |
| `antiflag.*` | Cookie/storage wipe, human typing, ẩn webdriver |

Biến môi trường:

| Env | Ý nghĩa |
|-----|---------|
| `GROK_NO_FOCUS=1` | Web tick “Ẩn Chrome” |
| `GROK_SUB2API=0` | Tắt import job này |
| `GROK_SKIP_KILL_OLD=1` | Không kill `main.py`/Chrome khác |
| `GROK_CHROME_PORT` | Override debug port |
| `GROK_CHROME_PROFILE` | Override user-data-dir |
| `GROK_THREADS` | Số worker (cẩn thận) |

## Turnstile

| Key | Ý nghĩa |
|-----|---------|
| `turnstile.mode` | `auto` |
| `turnstile.solver_url` | `http://127.0.0.1:5072` |
| `turnstile.timeout_sec` | Timeout mỗi lần solve |
| `turnstile.yescaptcha_key` | Để trống nếu chỉ dùng solver local |
| `turnstile.sitekey` | Để trống = tự detect |
| `cf_max_retries` / `cf_wait_sec` | Cloudflare browser path |
| `castle_warmup_sec` / `castle_wait_token_sec` | Castle token (browser) |

## Sub2API

| Key | Ý nghĩa |
|-----|---------|
| `sub2api.enabled` | Bật import sau reg |
| `sub2api.mode` | `auto` |
| `sub2api.sub2api_url` | Base URL local/remote **của bạn** |
| `sub2api.sub2api_user` / `sub2api_pass` | Login admin |
| `sub2api.sub2api_api_token` | Token sẵn (nếu có) |
| `sub2api.name_prefix` | `grok free` → `grok free 001` |
| `sub2api.start_number` | Số bắt đầu |
| `sub2api.group` | Tên group, mặc định `grok free` |
| `sub2api.group_ids` | ID group nếu API cần |
| `sub2api.timeout_oauth_sec` | Timeout SSO/OAuth |
| `sub2api.fallback_browser_oauth` | Fallback mở browser nếu API fail |
| `sub2api.durable_retry` | Queue retry khi import fail |
| `sub2api.run_test` | Gọi test model sau import (thường `false`) |

## Google Sheets

Tắt mặc định. Chỉ điền khi tự setup.

| Key | Ý nghĩa |
|-----|---------|
| `google_sheets.enabled` | `false` |
| `spreadsheet_id` | Để trống trên GitHub |
| `gid` | Tab id, `0` = sheet đầu |
| `webapp_url` / `webapp_secret` | Apps Script deploy của bạn |
| `credentials_file` | Service account local, gitignored |
| `mode` | `replace_night` |
| `require_sheet_success` | `false` = reg không fail vì sheet |

## Proxy

`proxy`: chuỗi rỗng = không proxy. Điền URL proxy **của bạn** nếu cần (không commit).
