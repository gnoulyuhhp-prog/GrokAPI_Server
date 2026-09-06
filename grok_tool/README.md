# ⚡ GROKREG — grok_tool Core Package

Package chính chứa toàn bộ mã nguồn của hệ thống **GROKREG** (Protocol HTTP, Stealth Chrome, Temp Mail Racing, Hotmail Graph API, Sub2API Integration).

> 📖 **Xem tài liệu hướng dẫn đầy đủ với giao diện chi tiết tại:** [README.md gốc của dự án](../README.md).

---

## 🚀 Khởi chạy nhanh

### Cách 1: Chạy 1-Click (Khuyên dùng)
Bấm đúp file `start.bat` ở thư mục gốc hoặc chạy:
```bat
start.bat
```

### Cách 2: Chạy Web Control Plane
```bat
venv\Scripts\python.exe -m web_console.app
# Mở trình duyệt tại: http://127.0.0.1:8787
```

### Cách 3: Chạy dòng lệnh (CLI)
```bat
# Protocol HTTP (~30s) với Temp Mail Racing:
venv\Scripts\python.exe main.py 4 --count 10 --backend protocol

# Hotmail:
venv\Scripts\python.exe main.py 1 --count 5 --backend protocol
```

---

## 📚 Tài liệu chi tiết
- [Cấu hình chi tiết (`config.json`)](docs/CONFIG.md)
- [Hướng dẫn sử dụng toàn diện](docs/USAGE.md)
- [Kiến trúc hệ thống](ARCHITECTURE.md)
- [Bảo mật dữ liệu](SAFE_GITHUB.md)
