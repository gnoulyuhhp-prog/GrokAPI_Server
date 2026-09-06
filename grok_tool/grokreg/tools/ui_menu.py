#!/usr/bin/env python3
"""
Menu UI cho Grok Register — style gần terminal mẫu (màu + icon).
Chỉ điều khiển: chọn mail / số lượng / stop loop → gọi main.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

try:
    from colorama import Fore, Style, init as colorama_init

    colorama_init(autoreset=True)
except Exception:  # pragma: no cover

    class _D:
        def __getattr__(self, _: str) -> str:
            return ""

    Fore = Style = _D()  # type: ignore


def _utf8() -> None:
    for s in (sys.stdout, sys.stderr, sys.stdin):
        try:
            if hasattr(s, "reconfigure"):
                s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def clear() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def line(char: str = "─", n: int = 56) -> None:
    print(f"{Fore.CYAN}{char * n}{Style.RESET_ALL}")


def title(text: str) -> None:
    print()
    line("═")
    print(f"{Fore.CYAN}{Style.BRIGHT}  {text}{Style.RESET_ALL}")
    line("═")
    print()


def ask(prompt: str) -> str:
    try:
        return input(f"{Fore.YELLOW}{prompt}{Style.RESET_ALL}").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(0)


def info(msg: str) -> None:
    print(f"{Fore.WHITE}  {msg}{Style.RESET_ALL}")


def ok(msg: str) -> None:
    print(f"{Fore.GREEN}  ✅ {msg}{Style.RESET_ALL}")


def err(msg: str) -> None:
    print(f"{Fore.RED}  ❌ {msg}{Style.RESET_ALL}")


def warn(msg: str) -> None:
    print(f"{Fore.YELLOW}  ⚠  {msg}{Style.RESET_ALL}")


def hotmail_count() -> int:
    p = ROOT / "data" / "hotmails.txt"
    if not p.exists():
        return 0
    try:
        from grokreg.core.config import load_config
        from grokreg.mail.providers import HotmailProvider

        slots, lines = HotmailProvider.from_config(p, load_config()).available_count()
        return slots or lines
    except Exception:
        n = 0
        for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = ln.strip()
            if s and not s.startswith("#"):
                n += 1
        return n


def py_exe() -> Path:
    if os.name == "nt":
        return ROOT / "venv" / "Scripts" / "python.exe"
    return ROOT / "venv" / "bin" / "python"


def run_main(mail_code: str, count: int) -> int:
    exe = py_exe()
    if not exe.exists():
        err("Chưa có venv. Chạy start.bat lần đầu để cài.")
        return 1
    # kill old
    kill = ROOT / "kill_old.bat"
    if kill.exists():
        subprocess.run(
            ["cmd", "/c", str(kill)],
            cwd=str(ROOT),
            shell=False,
        )
    stop = ROOT / "data" / "STOP"
    if stop.exists():
        try:
            stop.unlink()
        except Exception:
            pass

    cmd = [str(exe), "-u", "main.py", mail_code, "--count", str(count)]
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    print()
    line()
    print(
        f"{Fore.CYAN}  ▶  Chạy: main.py {mail_code} --count {count}{Style.RESET_ALL}"
    )
    line()
    print()
    r = subprocess.run(cmd, cwd=str(ROOT), env=env)
    return int(r.returncode or 0)


def send_stop() -> None:
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "STOP").write_text("stop:menu\n", encoding="utf-8")
    ok("Đã gửi lệnh DỪNG (data/STOP).")
    info("Tip: trong cửa sổ đang reg, nhấn ESC cũng dừng ngay.")


def make_shortcut() -> None:
    target = ROOT / "CHAY_REG.bat"
    desktop = Path(os.environ.get("USERPROFILE", "")) / "Desktop"
    if not desktop.is_dir():
        desktop = Path(os.environ.get("USERPROFILE", "")) / "OneDrive" / "Desktop"
    if not desktop.is_dir():
        err("Không tìm thấy Desktop")
        return
    lnk = desktop / "Grok Register.lnk"
    ps = f"""
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut(r'{lnk}')
$s.TargetPath = r'{target}'
$s.WorkingDirectory = r'{ROOT}'
$s.WindowStyle = 1
$s.Description = 'Grok Register'
$s.Save()
Write-Host 'OK'
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True,
    )
    ok(f"Shortcut: {lnk}")


MAIL_MAP = {
    "0": ("Temp SMART (azpop ↔ tmail)", "0"),
    "1": ("Hotmail", "1"),
    "2": ("Temp azpop only", "2"),
    "3": ("Temp tmail.wibu only", "3"),
    "4": ("Temp Racing (TinyHost/Lol/VIP)", "4"),
}


def pick_mail() -> str | None:
    title("Bước 1/2 — Loại email")
    print(f"  {Fore.CYAN}[0]{Style.RESET_ALL}  Temp SMART     {Fore.MAGENTA}(khuyên dùng){Style.RESET_ALL}")
    print(f"  {Fore.CYAN}[1]{Style.RESET_ALL}  Hotmail        1 acc → tối đa 5 Grok (+1…+4)")
    print(f"  {Fore.CYAN}[2]{Style.RESET_ALL}  Temp azpop     only")
    print(f"  {Fore.CYAN}[3]{Style.RESET_ALL}  Temp tmail     only")
    print(f"  {Fore.CYAN}[4]{Style.RESET_ALL}  Temp Racing    TinyHost / TempMail.lol / VIP")
    print()
    print(f"  {Fore.WHITE}[B]{Style.RESET_ALL}  Quay lại menu")
    print()
    while True:
        ans = ask("  Chọn mail [0-4 / B]: ") or "0"
        if ans.lower() == "b":
            return None
        if ans in MAIL_MAP:
            if ans == "1" and hotmail_count() == 0:
                err("data\\hotmails.txt đang RỖNG — thêm hotmail rồi chọn lại.")
                ask("  Enter để tiếp tục...")
                return pick_mail()
            return ans
        err("Chỉ nhập 0, 1, 2, 3, 4 hoặc B.")


def pick_count(mail_code: str) -> int | None:
    title("Bước 2/2 — Số lượng acc")
    print(f"  {Fore.GREEN}1 / 5 / 20{Style.RESET_ALL}  = reg đúng số đó")
    print(f"  {Fore.YELLOW}0{Style.RESET_ALL}           = chạy LIÊN TỤC đến khi DỪNG")
    print()
    print(f"  {Fore.YELLOW}Dừng nhanh:{Style.RESET_ALL} nhấn {Fore.RED}ESC{Style.RESET_ALL} trong cửa sổ đang reg")
    print(f"             hoặc {Fore.CYAN}Ctrl+C{Style.RESET_ALL} / menu [2] (data/STOP)")
    print()
    if mail_code == "1":
        print(f"  Hotmail pool: {Fore.MAGENTA}{hotmail_count()}{Style.RESET_ALL} dòng")
        print()
    print(f"  {Fore.WHITE}[B]{Style.RESET_ALL}  Quay lại")
    print()
    while True:
        ans = ask("  Nhập số lượng [Enter=1, 0=liên tục]: ") or "1"
        if ans.lower() == "b":
            return None
        if not ans.isdigit():
            err("Phải nhập số (0 = liên tục, 1–99 = số lượng).")
            continue
        n = int(ans)
        if n > 99:
            err("Tối đa 99 / lần (hoặc 0 = liên tục).")
            continue
        if mail_code == "1" and n > 0 and n > hotmail_count():
            warn(f"Chọn {n} nhưng Hotmail chỉ có {hotmail_count()} — vẫn chạy.")
        return n


def main_menu() -> None:
    while True:
        clear()
        title("GROK REGISTER TOOL")
        print(f"  {Fore.WHITE}Folder:{Style.RESET_ALL} {ROOT}")
        print()
        print(f"  {Fore.CYAN}{Style.BRIGHT}[1]{Style.RESET_ALL}  Reg acc          (chọn mail + số lượng)")
        print(f"  {Fore.GREEN}{Style.BRIGHT}[2]{Style.RESET_ALL}  Web UI xịn       (http://127.0.0.1:8787)")
        print(f"  {Fore.YELLOW}{Style.BRIGHT}[3]{Style.RESET_ALL}  DỪNG loop        (khi đang chạy liên tục)")
        print(f"  {Fore.WHITE}[4]{Style.RESET_ALL}  Tạo shortcut Desktop")
        print(f"  {Fore.WHITE}[0]{Style.RESET_ALL}  Thoát")
        print()
        line("═")
        print()
        ans = ask("  Chọn [0-4]: ") or "1"

        if ans == "0":
            print(f"\n{Fore.WHITE}  Bye.{Style.RESET_ALL}\n")
            return
        if ans == "2":
            clear()
            title("WEB CONTROL PLANE")
            info("Mở UI multi-tool (Aurora) tại http://127.0.0.1:8787/")
            print()
            conf = ask("  Enter = mở web  |  B = hủy: ")
            if conf.lower() != "b":
                bat = ROOT / "CHAY_WEB.bat"
                if bat.exists():
                    if os.name == "nt":
                        subprocess.Popen(
                            ["cmd", "/c", "start", "", str(bat)],
                            cwd=str(ROOT),
                            shell=False,
                        )
                        ok("Đã mở CHAY_WEB.bat (cửa sổ mới).")
                    else:
                        exe = py_exe()
                        subprocess.Popen(
                            [str(exe), "-m", "web_console.app"],
                            cwd=str(ROOT),
                        )
                        ok("Đã start web_console.")
                else:
                    err("Thiếu CHAY_WEB.bat")
            ask("  Enter để về menu...")
            continue
        if ans == "3":
            clear()
            title("DỪNG LOOP")
            send_stop()
            ask("  Enter để về menu...")
            continue
        if ans == "4":
            clear()
            title("SHORTCUT")
            make_shortcut()
            ask("  Enter để về menu...")
            continue
        if ans != "1":
            err("Chỉ nhập 0–4.")
            ask("  Enter...")
            continue

        # Reg flow
        while True:
            clear()
            mail = pick_mail()
            if mail is None:
                break
            clear()
            count = pick_count(mail)
            if count is None:
                continue

            label = MAIL_MAP[mail][0]
            clear()
            title("XÁC NHẬN & CHẠY")
            print(f"  {Fore.CYAN}Email   :{Style.RESET_ALL} {Fore.MAGENTA}{label}{Style.RESET_ALL}  (code {mail})")
            if count == 0:
                print(f"  {Fore.CYAN}Số lượng:{Style.RESET_ALL} {Fore.YELLOW}LIÊN TỤC{Style.RESET_ALL} đến khi DỪNG")
                print(f"  {Fore.CYAN}Dừng    :{Style.RESET_ALL} {Fore.RED}ESC{Style.RESET_ALL} / Ctrl+C / menu [2]")
            else:
                print(f"  {Fore.CYAN}Số lượng:{Style.RESET_ALL} {Fore.GREEN}{count}{Style.RESET_ALL} acc")
                print(f"  {Fore.CYAN}Dừng    :{Style.RESET_ALL} {Fore.RED}ESC{Style.RESET_ALL} trong cửa sổ reg (dừng ngay)")
            print(f"  {Fore.CYAN}Guest   :{Style.RESET_ALL} xóa session acc cũ mỗi lần reg")
            print()
            conf = ask("  Enter = chạy  |  B = hủy: ")
            if conf.lower() == "b":
                break

            code = run_main(mail, count)
            print()
            line("═")
            if code == 0:
                ok(f"Xong. Exit code: {code}")
            else:
                warn(f"Xong. Exit code: {code}")
            print(f"  {Fore.WHITE}Kết quả → Google Sheet (tab grok){Style.RESET_ALL}")
            line("═")
            print()
            again = ask("  Enter = menu  |  X = thoát: ")
            if again.lower() == "x":
                return
            break


if __name__ == "__main__":
    _utf8()
    try:
        main_menu()
    except SystemExit:
        raise
    except Exception as e:
        err(str(e))
        try:
            input("Enter...")
        except Exception:
            pass
        raise SystemExit(1)
