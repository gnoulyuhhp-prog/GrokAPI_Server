import sys
import os
import json
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import requests

ROOT = Path(__file__).resolve().parent
config_path = ROOT / "config.json"

if not config_path.exists():
    print("[x] Chưa tìm thấy file config.json! Hãy tạo hoặc cấu hình config.json.")
    sys.exit(1)

with open(config_path, "r", encoding="utf-8") as f:
    cfg = json.load(f)

sub_cfg = cfg.get("sub2api", {})
enabled = sub_cfg.get("enabled", False)
configured_url = sub_cfg.get("sub2api_url", "https://grokapi.vorte.me").rstrip("/")
email = sub_cfg.get("sub2api_user", "")
password = sub_cfg.get("sub2api_pass", "")
api_token = sub_cfg.get("sub2api_api_token", "")
group = sub_cfg.get("group", "grok free")

print("=" * 65)
print("  KIỂM TRA LIÊN KẾT: REG GROK MƯỢT <---> SUB2API GATEWAY")
print("=" * 65)
print(f" * Trạng thái Sub2API trong config: {'BẬT (True)' if enabled else 'TẮT (False)'}")
print(f" * URL đang cấu hình: {configured_url}")
print(f" * Nhóm đích (Group): {group}")
print("-" * 65)

# Danh sách URL cần scan
candidate_urls = [configured_url]
for u in ["https://grokapi.vorte.me", "http://127.0.0.1:8082", "http://127.0.0.1:8081", "http://127.0.0.1:8080"]:
    if u not in candidate_urls:
        candidate_urls.append(u)

active_url = None
for u in candidate_urls:
    try:
        resp = requests.get(f"{u}/health", timeout=3)
        if resp.status_code == 200:
            active_url = u
            break
    except Exception:
        pass
    try:
        resp = requests.post(f"{u}/api/v1/auth/login", json={}, timeout=3)
        if resp.status_code in (200, 400, 401):
            active_url = u
            break
    except Exception:
        pass

if not active_url:
    print(" [!] [1/3] Chưa kết nối được tới Sub2API Gateway!")
    print(f"     Đã thử: {', '.join(candidate_urls)}")
else:
    label = "VPS Cloud 24/7" if "grokapi.vorte.me" in active_url else ("VPS Tunnel" if ":8082" in active_url else "Local")
    print(f" [OK] [1/3] Đã kết nối thành công tới Sub2API ({label}) tại: {active_url}")
    if active_url != configured_url:
        print(f"     ⚠️ Lưu ý: Config đang là {configured_url}, nên sửa thành {active_url}")

    # 2. Kiểm tra xác thực
    token = None
    if api_token:
        print(" * Đang thử xác thực bằng API Token...")
        headers = {"x-api-key": api_token}
        try:
            resp = requests.get(f"{active_url}/api/v1/admin/groups/all", headers=headers, timeout=4)
            if resp.status_code == 200:
                print(" [OK] [2/3] Xác thực Admin bằng API Token thành công!")
                token = api_token
            else:
                print(f" [!] API Token trả về HTTP {resp.status_code}")
        except Exception as e:
            print(f" [!] Lỗi API Token: {e}")

    if not token and email and password and email != "YOUR_SUB2API_EMAIL":
        print(f" * Đang thử đăng nhập với user: {email}...")
        try:
            resp = requests.post(f"{active_url}/api/v1/auth/login", json={"email": email, "password": password}, timeout=4)
            if resp.status_code == 200:
                data = resp.json()
                inner = data.get("data", data)
                token = inner.get("access_token") or inner.get("token")
                print(" [OK] [2/3] Đăng nhập Admin qua Email/Password thành công!")
            else:
                print(f" [!] Đăng nhập chưa thành công (HTTP {resp.status_code})")
                print("     👉 Vui lòng điền đúng 'sub2api_user' và 'sub2api_pass' của VPS vào config.json")
        except Exception as e:
            print(f" [!] Lỗi khi gửi request login: {e}")
    elif not token:
        print(" [i] [2/3] sub2api_user/pass đang để mẫu trong grok_tool/config.json.")
        print("     👉 Hãy điền tài khoản đăng nhập admin Sub2API của bạn để tự động nạp acc.")

    # 3. Kiểm tra group
    if token:
        try:
            headers = {"x-api-key": token} if token == api_token else {"Authorization": f"Bearer {token}"}
            resp = requests.get(f"{active_url}/api/v1/admin/groups/all", headers=headers, timeout=4)
            if resp.status_code == 200:
                res_json = resp.json()
                groups = res_json.get("data", res_json) if isinstance(res_json, dict) else res_json
                if isinstance(groups, list):
                    g_names = [g.get("name") for g in groups if isinstance(g, dict)]
                    print(f" [OK] [3/3] Danh sách Groups trên Sub2API: {g_names}")
                    if group in g_names:
                        print(f" [OK] Group '{group}' ĐÃ SẴN SÀNG tiếp nhận tài khoản Grok mới!")
                    else:
                        print(f" [i] Chưa có group '{group}' trên Sub2API (Bạn có thể tạo group '{group}' trên web Sub2API).")
        except Exception as e:
            print(f" [!] Lỗi lấy danh sách groups: {e}")

print("=" * 65)
print(" Luồng hoạt động: Tool reg xong -> Cookie SSO tự động bắn lên VPS!")
print("=" * 65)
