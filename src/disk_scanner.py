"""
Сканер диска C: — установленные приложения, временные файлы и большие файлы (> 500 МБ).
Системные папки и файлы с атрибутом "системный" не сканируются и не удаляются.
Требуется только стандартная библиотека Python 3 (tkinter, winreg, ctypes).
"""

import ctypes
import os
import queue
import random
import stat
import subprocess
import sys
import threading
import time
import tkinter as tk
import winreg
from ctypes import wintypes
from tkinter import ttk

DRIVE = "C:\\"
BIG_FILE_LIMIT = 500 * 1024 * 1024
TEMP_MIN_AGE_SEC = 60 * 60  # не трогаем временные файлы моложе часа — они могут быть в использовании

FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

WINDIR = os.environ.get("WINDIR", r"C:\Windows")
LOCAL = os.environ.get("LOCALAPPDATA", "")
TEMP = os.environ.get("TEMP", "")

# Папки, в которые сканер больших файлов никогда не заходит
SYSTEM_DIRS = {
    os.path.normcase(p)
    for p in (
        WINDIR,
        r"C:\$Recycle.Bin",
        r"C:\System Volume Information",
        r"C:\Recovery",
        r"C:\Config.Msi",
        r"C:\Boot",
        r"C:\$WinREAgent",
        r"C:\$Windows.~BT",
        r"C:\$Windows.~WS",
        r"C:\$SysReset",
        r"C:\Documents and Settings",
    )
}
# Папки программ — пропускаются, если не включена соответствующая галочка
PROGRAM_DIRS = {
    os.path.normcase(p)
    for p in (
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramData", r"C:\ProgramData"),
    )
}
SYSTEM_FILE_NAMES = {"pagefile.sys", "hiberfil.sys", "swapfile.sys", "dumpstack.log", "dumpstack.log.tmp"}


def human_size(n):
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return f"{n:.1f} {unit}" if unit != "Б" else f"{int(n)} {unit}"
        n /= 1024


def is_system_entry(entry):
    try:
        attrs = entry.stat(follow_symlinks=False).st_file_attributes
    except OSError:
        return True
    return bool(attrs & FILE_ATTRIBUTE_SYSTEM)


def is_reparse_point(entry):
    try:
        return bool(entry.stat(follow_symlinks=False).st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return True


# ---------- Корзина (SHFileOperationW) ----------

class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def move_to_recycle_bin(path):
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = path + "\0"  # строка должна заканчиваться двумя нулями
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0 and not op.fAnyOperationsAborted


# ---------- Установленные приложения ----------

UNINSTALL_KEYS = [
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_64KEY),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_32KEY),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0),
]
HIVE_NAMES = {winreg.HKEY_LOCAL_MACHINE: "HKLM", winreg.HKEY_CURRENT_USER: "HKCU"}
HIVES = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}
VIEW_FLAGS = {"64": winreg.KEY_WOW64_64KEY, "32": winreg.KEY_WOW64_32KEY, "": 0}


def read_value(key, name, default=""):
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return default


def get_installed_apps():
    apps = {}
    for hive, path, flag in UNINSTALL_KEYS:
        try:
            root = winreg.OpenKey(hive, path, 0, winreg.KEY_READ | flag)
        except OSError:
            continue
        with root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    subkey = winreg.EnumKey(root, i)
                    with winreg.OpenKey(root, subkey) as k:
                        name = read_value(k, "DisplayName")
                        # Пропускаем системные компоненты и обновления
                        if not name or read_value(k, "SystemComponent", 0) == 1 or read_value(k, "ParentKeyName"):
                            continue
                        size_kb = read_value(k, "EstimatedSize", 0) or 0
                        install_date = str(read_value(k, "InstallDate"))
                        if len(install_date) == 8 and install_date.isdigit():
                            install_date = f"{install_date[6:]}.{install_date[4:6]}.{install_date[:4]}"
                        apps[(name, read_value(k, "DisplayVersion"))] = {
                            "name": name,
                            "publisher": read_value(k, "Publisher"),
                            "version": read_value(k, "DisplayVersion"),
                            "size": int(size_kb) * 1024 if isinstance(size_kb, int) else 0,
                            "date": install_date,
                            "location": read_value(k, "InstallLocation"),
                            "uninstall": read_value(k, "UninstallString"),
                            "icon": read_value(k, "DisplayIcon"),
                            "msi": read_value(k, "WindowsInstaller", 0) == 1,
                            "subkey": subkey,
                            "reg_key": f"{HIVE_NAMES[hive]}\\{path}\\{subkey}",
                            "reg_view": {winreg.KEY_WOW64_64KEY: "64", winreg.KEY_WOW64_32KEY: "32"}.get(flag, ""),
                        }
                except OSError:
                    continue
    return list(apps.values())


# ---------- Полное (принудительное) удаление программ ----------

APPDATA = os.environ.get("APPDATA", "")
PROGRAMDATA = os.environ.get("ProgramData", r"C:\ProgramData")
USERPROFILE = os.environ.get("USERPROFILE", "")
PUBLIC = os.environ.get("PUBLIC", r"C:\Users\Public")
START_MENUS = [os.path.join(PROGRAMDATA, r"Microsoft\Windows\Start Menu\Programs"),
               os.path.join(APPDATA, r"Microsoft\Windows\Start Menu\Programs")]
DESKTOPS = [os.path.join(USERPROFILE, "Desktop"), os.path.join(PUBLIC, "Desktop")]

# Имена папок, которые слишком общие, чтобы удалять их целиком
GENERIC_NAMES = {
    "", "windows", "system32", "program files", "program files (x86)", "programdata", "common files", "users",
    "appdata", "local", "roaming", "locallow", "programs", "microsoft", "google", "temp", "packages", "bin", "app",
    "apps", "games", "tools", "software", "data", "default", "public", "desktop", "documents", "downloads",
    "start menu", "startup", "microsoft shared", "windowsapps", "installer", "intel", "nvidia", "nvidia corporation",
    "amd", "drivers", "steam", "steamapps", "common", "x86", "x64", "uninstall", "updates", "update", "cache",
}
SHARED_PARTS = {"common", "common files", "shared", "microsoft shared", "plug-ins", "plugins", "plug-in",
                "mediacore", "redist", "redistributables", "system32", "syswow64", "drivers", "spool",
                "package cache", "installshield installation information"}
# Слова, по которым нельзя судить о принадлежности папки программе
STOP_WORDS = {"for", "the", "and", "with", "version", "edition", "desktop", "update", "x64", "x86", "bit", "pro",
              "inc", "ltd", "llc", "corp", "corporation", "company", "software", "technologies", "repack",
              "setup", "free", "beta", "series", "driver", "tools", "studio", "app", "client", "service"}
UNINSTALLER_DIRS = {"uninstall", "uninst", "_uninstall", "uninstaller", "bin", "x64", "x86", "64bit", "32bit"}

PROTECTED_DIRS = {
    os.path.normcase(os.path.abspath(p)).rstrip("\\") for p in (
        WINDIR, PROGRAMDATA, USERPROFILE, PUBLIC, APPDATA, LOCAL, os.path.join(LOCAL, "Programs"),
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramW6432", r"C:\Program Files"),
        os.environ.get("CommonProgramFiles", r"C:\Program Files\Common Files"),
        os.environ.get("CommonProgramFiles(x86)", r"C:\Program Files (x86)\Common Files"),
        os.path.join(PROGRAMDATA, "Microsoft"), os.path.join(APPDATA, "Microsoft"), os.path.join(LOCAL, "Microsoft"),
        os.path.join(os.path.dirname(USERPROFILE) or r"C:\Users"), *START_MENUS, *DESKTOPS,
    ) if p
}


def norm(path):
    return os.path.normcase(os.path.abspath(path)).rstrip("\\")


def is_protected_path(path):
    """True для путей, удаление которых может повредить Windows или другие программы."""
    p = norm(path)
    if len(p) <= 3 or p in PROTECTED_DIRS:
        return True
    if p == norm(WINDIR) or p.startswith(norm(WINDIR) + "\\"):
        return True
    if os.path.basename(p) in GENERIC_NAMES:
        return True
    # Общие папки, которыми пользуются несколько программ (например, Adobe\Common\Plug-ins)
    if any(part in SHARED_PARTS for part in p.split("\\")[1:]):
        return True
    # Не трогаем сами папки профилей пользователей (C:\Users\<имя>)
    users = norm(os.path.dirname(USERPROFILE) or r"C:\Users")
    return os.path.dirname(p) == users


def path_from_command(cmd):
    """Извлекает путь к exe из строки вида '"C:\\x\\a.exe" /arg' или 'C:\\x\\a.exe,0'."""
    cmd = os.path.expandvars(str(cmd or "")).strip()
    if not cmd:
        return ""
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        return cmd[1:end] if end > 0 else cmd[1:]
    low = cmd.lower()
    idx = low.find(".exe")
    if idx >= 0:
        return cmd[:idx + 4]
    return cmd.split(",")[0].strip()


def app_folder(app):
    """Папка программы: InstallLocation, а если её нет — папка иконки или деинсталлятора."""
    loc = os.path.expandvars(str(app.get("location") or "")).strip().strip('"').rstrip("\\")
    while loc and os.path.basename(loc).lower() in UNINSTALLER_DIRS:
        loc = os.path.dirname(loc)
    candidates = [loc] if loc else []
    for cmd in (app.get("icon"), app.get("uninstall")):
        exe = path_from_command(cmd)
        if exe and "msiexec" not in exe.lower():
            d = os.path.dirname(exe)
            while os.path.basename(d).lower() in UNINSTALLER_DIRS:
                d = os.path.dirname(d)
            candidates.append(d)
    existing = [os.path.abspath(c).rstrip("\\") for c in candidates if c and os.path.isdir(c)]
    # предпочитаем незащищённую папку; защищённую возвращаем только чтобы сообщить о ней
    return next((c for c in existing if not is_protected_path(c)), existing[0] if existing else "")


def _alnum(s):
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def folder_matches_app(folder, app):
    """Проверяет, что имя папки похоже на название программы или издателя —
    чтобы не удалить «D:\\Новая папка» или чужой каталог, указанный в реестре по ошибке."""
    parts = [p for p in norm(folder).split("\\")[1:] if p][-2:]
    tail = _alnum("".join(parts))
    words = set()
    for text in (app["name"], app.get("publisher") or ""):
        cur = ""
        for ch in str(text).lower() + " ":
            if ch.isalnum():
                cur += ch
            else:
                if len(cur) >= 3 and cur not in STOP_WORDS and not cur.isdigit():
                    words.add(cur)
                cur = ""
    if any(w in tail for w in words):
        return True
    base = _alnum(os.path.basename(folder))
    return len(base) >= 3 and base in _alnum(app["name"])


def pack_guid(guid):
    """{12345678-ABCD-...} -> формат, в котором Windows Installer хранит код продукта в реестре."""
    g = guid.strip("{}").replace("-", "").upper()
    if len(g) != 32:
        return None
    return g[:8][::-1] + g[8:12][::-1] + g[12:16][::-1] + "".join(g[i + 1] + g[i] for i in range(16, 32, 2))


def reg_exists(full_key, view=""):
    hive, _, sub = full_key.partition("\\")
    try:
        winreg.CloseKey(winreg.OpenKey(HIVES[hive], sub, 0, winreg.KEY_READ | VIEW_FLAGS[view]))
        return True
    except OSError:
        return False


def find_services(folder):
    prefix = norm(folder) + "\\"
    found = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services")
    except OSError:
        return found
    with root:
        for i in range(winreg.QueryInfoKey(root)[0]):
            try:
                name = winreg.EnumKey(root, i)
                with winreg.OpenKey(root, name) as k:
                    img = os.path.expandvars(str(read_value(k, "ImagePath"))).strip().lstrip('"')
            except OSError:
                continue
            if img.startswith("\\??\\"):
                img = img[4:]
            if img and os.path.normcase(img).startswith(prefix):
                found.append(name)
    return found


def find_processes(folder):
    """PID процессов, запущенных из папки программы."""
    prefix = norm(folder) + "\\"
    pids = (wintypes.DWORD * 4096)()
    needed = wintypes.DWORD()
    if not ctypes.windll.psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
        return []
    result = []
    k32 = ctypes.windll.kernel32
    for pid in pids[:needed.value // ctypes.sizeof(wintypes.DWORD)]:
        if not pid or pid == os.getpid():
            continue
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            continue
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                if os.path.normcase(buf.value).startswith(prefix):
                    result.append((pid, os.path.basename(buf.value)))
        finally:
            k32.CloseHandle(h)
    return result


def plan_force_removal(app, all_apps):
    """Собирает всё, что относится к программе: папки, службы, ярлыки, ключи реестра."""
    plan = {"folder": "", "notes": [], "folders": [], "services": [], "shortcuts": [], "reg": []}
    other_dirs = set()
    for a in all_apps:
        if a is not app and a["name"] != app["name"]:
            d = app_folder(a)
            if d and not is_protected_path(d):
                other_dirs.add(norm(d))

    def safe_dir(path):
        p = norm(path)
        if is_protected_path(path):
            return False
        # папка не должна содержать другие программы и не должна быть их папкой
        return not any(o == p or o.startswith(p + "\\") for o in other_dirs)

    folder = app_folder(app)
    if folder and safe_dir(folder) and not folder_matches_app(folder, app):
        plan["notes"].append(f"Имя папки {folder} не похоже на название программы, поэтому она НЕ будет "
                             "удалена автоматически. Если уверены, что это папка программы, удалите её вручную.")
        folder = ""
    elif folder:
        if safe_dir(folder):
            plan["folder"] = folder
            plan["services"] = find_services(folder)
        else:
            plan["notes"].append(f"Папка {folder} — системная, общая или содержит другие программы, "
                                 "поэтому она НЕ будет удалена.")
    else:
        plan["notes"].append("Папка программы не найдена (возможно, уже удалена).")

    # Остатки в AppData / ProgramData — только точное совпадение имени папки
    names = {app["name"].strip()}
    if folder:
        names.add(os.path.basename(folder))
    names = {n for n in names if n.lower() not in GENERIC_NAMES and len(n) > 2}
    publisher = str(app.get("publisher") or "").strip()
    for base in (APPDATA, LOCAL, PROGRAMDATA, os.path.join(os.path.dirname(LOCAL), "LocalLow")):
        for n in names:
            for p in (os.path.join(base, n), os.path.join(base, publisher, n) if publisher else ""):
                if p and os.path.isdir(p) and norm(p) != norm(folder or "?") and safe_dir(p):
                    if norm(p) not in {norm(x) for x in plan["folders"]}:
                        plan["folders"].append(p)

    # Ярлыки в меню «Пуск» и на рабочем столе
    lnk_names = {n.lower() for n in names}
    for base in START_MENUS:
        for n in names:
            p = os.path.join(base, n)
            if os.path.isdir(p) and safe_dir(p) and p not in plan["folders"]:
                plan["folders"].append(p)
    for base in START_MENUS + DESKTOPS:
        try:
            for e in os.scandir(base):
                if e.is_file() and e.name.lower().endswith(".lnk") and e.name[:-4].lower() in lnk_names:
                    plan["shortcuts"].append(e.path)
        except OSError:
            continue

    # Реестр: ключ удаления, данные Windows Installer, ветки Software
    plan["reg"].append((app["reg_key"], app["reg_view"]))
    packed = pack_guid(app["subkey"]) if app.get("msi") else None
    if packed:
        for key in (rf"HKLM\SOFTWARE\Classes\Installer\Products\{packed}",
                    rf"HKLM\SOFTWARE\Classes\Installer\Features\{packed}",
                    rf"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Installer\UserData\S-1-5-18\Products\{packed}",
                    rf"HKCU\Software\Microsoft\Installer\Products\{packed}"):
            if reg_exists(key, "64"):
                plan["reg"].append((key, "64"))
    for n in names:
        for key in [rf"HKCU\Software\{n}", rf"HKLM\SOFTWARE\{n}", rf"HKLM\SOFTWARE\WOW6432Node\{n}"] + (
                [rf"HKCU\Software\{publisher}\{n}", rf"HKLM\SOFTWARE\{publisher}\{n}"]
                if publisher and publisher.lower() not in GENERIC_NAMES else []):
            if reg_exists(key, "64") and (key, "64") not in plan["reg"]:
                plan["reg"].append((key, "64"))
    return plan


def needs_admin(plan):
    if any(k.startswith("HKLM") for k, _ in plan["reg"]) or plan["services"]:
        return True
    prof = norm(USERPROFILE) + "\\"
    paths = ([plan["folder"]] if plan["folder"] else []) + plan["folders"] + plan["shortcuts"]
    return any(not norm(p).startswith(prof) for p in paths)


def run_cmd(line):
    """Выполняет команду через cmd.exe и возвращает (код, вывод)."""
    r = subprocess.run('cmd.exe /d /c "' + line + '"', capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    out = (r.stdout + r.stderr).decode("oem", errors="replace").strip()
    return r.returncode, out


def force_remove(plan, log):
    """Полное удаление по плану. log(text, ok) получает строки журнала."""
    def step(line, check_gone=None, optional=False):
        code, out = run_cmd(line)
        ok = code == 0 if check_gone is None else not os.path.exists(check_gone)
        if ok or not optional:
            log(f"{'[OK]' if ok else '[ОШИБКА]'} {line}" + (f"\n      {out}" if out and not ok else ""), ok)
        return ok

    folder = plan["folder"]
    if folder:
        for pid, exe in find_processes(folder):
            step(f"taskkill /F /T /PID {pid}")
            log(f"      процесс {exe}", True)
    for svc in plan["services"]:
        step(f'sc stop "{svc}"', optional=True)
        step(f'sc delete "{svc}"')
    if plan["services"]:
        time.sleep(1.5)  # даём службам завершиться перед удалением файлов
    for f in ([folder] if folder else []) + plan["folders"]:
        if not os.path.exists(f):
            continue
        run_cmd(f'attrib -r -s -h "{f}\\*" /s /d')
        if not step(f'rd /s /q "{f}"', check_gone=f):
            log("      часть файлов занята — перезагрузите компьютер и повторите удаление", False)
        parent = os.path.dirname(f)
        if not is_protected_path(parent) and os.path.isdir(parent) and not os.listdir(parent):
            step(f'rd "{parent}"', check_gone=parent, optional=True)  # пустая папка издателя
    for lnk in plan["shortcuts"]:
        step(f'del /f /q "{lnk}"', check_gone=lnk)
    for key, view in plan["reg"]:
        if reg_exists(key, view):
            step(f'reg delete "{key}" /f' + (f" /reg:{view}" if view else ""))


# ---------- Временные файлы ----------

def temp_locations():
    locs = [
        ("Временные файлы пользователя", TEMP),
        ("Временные файлы Windows", os.path.join(WINDIR, "Temp")),
        ("Кэш Internet Explorer / WinINet", os.path.join(LOCAL, r"Microsoft\Windows\INetCache")),
        ("Дампы падений программ", os.path.join(LOCAL, "CrashDumps")),
        ("Кэш эскизов проводника", os.path.join(LOCAL, r"Microsoft\Windows\Explorer")),
    ]
    browsers = {
        "Google Chrome": os.path.join(LOCAL, r"Google\Chrome\User Data"),
        "Microsoft Edge": os.path.join(LOCAL, r"Microsoft\Edge\User Data"),
        "Яндекс.Браузер": os.path.join(LOCAL, r"Yandex\YandexBrowser\User Data"),
        "Opera": os.path.join(LOCAL, r"Opera Software\Opera Stable"),
    }
    for browser, base in browsers.items():
        if not os.path.isdir(base):
            continue
        profiles = [base] + [os.path.join(base, d) for d in os.listdir(base)
                             if d == "Default" or d.startswith("Profile ")]
        for prof in profiles:
            for sub in ("Cache", "Code Cache", "GPUCache"):
                p = os.path.join(prof, sub)
                if os.path.isdir(p):
                    locs.append((f"Кэш {browser} ({os.path.basename(prof)}, {sub})", p))
    ff = os.path.join(LOCAL, r"Mozilla\Firefox\Profiles")
    if os.path.isdir(ff):
        for prof in os.listdir(ff):
            p = os.path.join(ff, prof, "cache2")
            if os.path.isdir(p):
                locs.append((f"Кэш Firefox ({prof})", p))
    return [(n, p) for n, p in locs if p and os.path.isdir(p)]


def iter_files(root, stop_event):
    """Обходит файлы без перехода по ссылкам/junction, пропуская системные."""
    stack = [root]
    while stack and not stop_event.is_set():
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if not is_reparse_point(e):
                                stack.append(e.path)
                        elif e.is_file(follow_symlinks=False) and not is_system_entry(e):
                            yield e
                    except OSError:
                        continue
        except OSError:
            continue


def is_thumbcache(path):
    name = os.path.basename(path).lower()
    return name.startswith(("thumbcache_", "iconcache_"))


def temp_filter(category, entry):
    # В папке Explorer удаляем только кэш эскизов, остальное не трогаем
    if category.startswith("Кэш эскизов"):
        return is_thumbcache(entry.path)
    return True


# ---------- Тема и визуальные эффекты ----------

BG = "#050a06"
PANEL = "#0a140d"
PANEL2 = "#0f1f14"
STRIPE = "#0c1910"
HOVER = "#123d22"
GREEN = "#00ff66"
GREEN_DIM = "#0e6b33"
GREEN_DARK = "#0a3a1d"
TEXT = "#b9f6ca"
MUTED = "#4f8f63"
WARN = "#ffc94d"
DANGER = "#ff5c7a"
FONT = "Consolas"


def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def hex_to_rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def blend(c1, c2, t):
    a, b = hex_to_rgb(c1), hex_to_rgb(c2)
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def ease_out(t):
    return 1 - (1 - t) ** 3


def fmt_int(v):
    return f"{int(round(v)):,}".replace(",", " ")


def dark_title_bar(win):
    """Тёмный заголовок окна (Windows 10 20H1+ / Windows 11)."""
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        on = ctypes.c_int(1)
        for attr in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                break
        caption = ctypes.c_int(0x00060A05)  # цвет заголовка в формате BGR (только Windows 11)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption))
    except Exception:
        pass


def fade_in(win, step=0.07, delay=15):
    def tick(a=0.0):
        a = min(1.0, a + step)
        try:
            win.attributes("-alpha", a)
        except tk.TclError:
            return
        if a < 1:
            win.after(delay, tick, a)
    tick()


class ColorAnimator:
    """Плавно переводит цветовые свойства виджета к новым значениям."""

    def __init__(self, widget):
        self.w = widget
        self.job = None

    def to(self, steps=8, delay=16, **targets):
        if self.job:
            self.w.after_cancel(self.job)
        start = {k: self.w.cget(k) for k in targets}

        def tick(i=1):
            t = i / steps
            try:
                self.w.configure(**{k: blend(start[k], v, t) for k, v in targets.items()})
            except tk.TclError:
                return
            self.job = self.w.after(delay, tick, i + 1) if i < steps else None
        tick()


class NeonButton(tk.Label):
    def __init__(self, master, text, command, accent=GREEN, bg=PANEL):
        super().__init__(master, text=text, bg=bg, fg=accent, font=(FONT, 10, "bold"), padx=14, pady=6,
                         cursor="hand2", highlightthickness=1, highlightbackground=GREEN_DARK,
                         highlightcolor=GREEN_DARK)
        self.accent, self.base_bg, self.command, self.enabled = accent, bg, command, True
        self.anim = ColorAnimator(self)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)

    def _enter(self, _=None):
        if self.enabled:
            self.anim.to(bg=blend(self.base_bg, self.accent, 0.18), highlightbackground=self.accent, fg=self.accent)

    def _leave(self, _=None):
        if self.enabled:
            self.anim.to(bg=self.base_bg, highlightbackground=GREEN_DARK, fg=self.accent)

    def _press(self, _):
        if self.enabled:
            self.configure(bg=self.accent, fg=BG)

    def _release(self, e):
        if not self.enabled:
            return
        inside = 0 <= e.x < self.winfo_width() and 0 <= e.y < self.winfo_height()
        self.configure(fg=self.accent)
        if inside:
            self._enter()
            self.command()
        else:
            self._leave()

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow",
                       fg=self.accent if enabled else GREEN_DIM, bg=self.base_bg, highlightbackground=GREEN_DARK)


class NeonCheck(tk.Label):
    def __init__(self, master, text, variable):
        super().__init__(master, bg=BG, fg=TEXT, font=(FONT, 10), cursor="hand2")
        self.text, self.var = text, variable
        self.anim = ColorAnimator(self)
        self.bind("<Button-1>", self._toggle)
        self.bind("<Enter>", lambda e: self.anim.to(fg=GREEN))
        self.bind("<Leave>", lambda e: self.anim.to(fg=TEXT))
        self._render()

    def _toggle(self, _):
        self.var.set(not self.var.get())
        self._render()

    def _render(self):
        self.configure(text=("[■] " if self.var.get() else "[ ] ") + self.text)


class MatrixHeader(tk.Canvas):
    """Шапка с «цифровым дождём», заголовком с эффектом глитча и индикатором заполнения диска."""

    CW, CH = 13, 16
    CHARS = "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉ0123456789ABCDEF<>/|=+*"

    def __init__(self, master, height=132):
        super().__init__(master, height=height, bg=BG, highlightthickness=0)
        self.levels = [blend(BG, GREEN, 0.85 * i / 15) for i in range(16)] + ["#e6fff0"]
        self.MAX = len(self.levels) - 1
        self.size = (0, 0)
        self.level = {}
        self.disk = (0, 1, 0)
        self.disk_shown = 0.0
        self.glitch_dx = 0
        self.scan_y = 0
        self.bind("<Configure>", self._on_resize)
        self.after(70, self._tick)
        self.after(2500, self._glitch)

    def _on_resize(self, e):
        if (e.width, e.height) == self.size:
            return
        self.size = (e.width, e.height)
        self.delete("all")
        self.cols = e.width // self.CW + 1
        self.rows = e.height // self.CH + 1
        self.cells = [[self.create_text(c * self.CW + 6, r * self.CH + 8, text=random.choice(self.CHARS),
                                        fill=BG, font=(FONT, 9)) for r in range(self.rows)]
                      for c in range(self.cols)]
        self.level = {}
        self.drops = [random.uniform(-self.rows * 2, 0) for _ in range(self.cols)]
        self.speeds = [random.uniform(0.25, 0.8) for _ in range(self.cols)]
        self.scanline = self.create_rectangle(0, 0, e.width, 2, fill=blend(BG, GREEN, 0.35), width=0,
                                              stipple="gray50")
        self._draw_overlay(e.width, e.height)

    def _draw_overlay(self, w, h):
        self.glitch_dx = 0
        # полупрозрачная подложка под текстом (растровая штриховка)
        self.create_rectangle(0, 0, 620, h, fill=BG, width=0, stipple="gray75")
        self.create_rectangle(w - 430, 0, w, h, fill=BG, width=0, stipple="gray75")
        self.create_line(0, h - 1, w, h - 1, fill=GREEN_DIM)
        title = "DISK SCANNER // C:"
        for dx, dy, col in ((-2, 0, GREEN_DARK), (2, 0, GREEN_DARK), (0, -2, GREEN_DARK), (0, 2, GREEN_DARK),
                            (-1, -1, GREEN_DIM), (1, 1, GREEN_DIM)):
            self.create_text(28 + dx, 44 + dy, text=title, anchor="w", fill=col,
                             font=(FONT, 28, "bold"), tags=("title",))
        self.create_text(28, 44, text=title, anchor="w", fill=GREEN, font=(FONT, 28, "bold"),
                         tags=("title", "title_main"))
        self.create_text(30, 86, anchor="w", fill=TEXT, font=(FONT, 10),
                         text="> приложения · временные файлы · большие файлы > 500 МБ")
        self.create_text(30, 106, anchor="w", fill=MUTED, font=(FONT, 9),
                         text="  системные файлы и папки Windows защищены от сканирования")
        # индикатор диска
        x0, x1 = w - 400, w - 28
        self.create_text(x0, 66, anchor="w", text="ЗАПОЛНЕНИЕ ДИСКА C:", fill=MUTED, font=(FONT, 9, "bold"))
        self.disk_pct = self.create_text(x1, 66, anchor="e", text="", fill=GREEN, font=(FONT, 9, "bold"))
        self.create_rectangle(x0, 78, x1, 94, outline=GREEN_DIM, width=1)
        self.disk_bar = self.create_rectangle(x0 + 2, 80, x0 + 2, 93, fill=GREEN, width=0)
        for i in range(1, 20):  # деления на шкале
            xx = x0 + (x1 - x0) * i / 20
            self.create_line(xx, 80, xx, 93, fill=BG)
        self.disk_text = self.create_text(x0, 108, anchor="w", text="", fill=TEXT, font=(FONT, 9))
        self.bar_x = (x0 + 2, x1 - 1)
        self._update_disk_text()

    def set_disk(self, used, total, free):
        self.disk = (used, total, free)
        if self.size[0]:
            self._update_disk_text()

    def _update_disk_text(self):
        used, total, free = self.disk
        self.itemconfigure(self.disk_text,
                           text=f"занято {human_size(used)} из {human_size(total)} · свободно {human_size(free)}")

    def _tick(self):
        if self.size[0]:
            for key, lv in list(self.level.items()):
                c, r = key
                lv -= 1
                if lv <= 0:
                    del self.level[key]
                    self.itemconfigure(self.cells[c][r], fill=BG)
                else:
                    self.level[key] = lv
                    self.itemconfigure(self.cells[c][r], fill=self.levels[lv])
            for c in range(self.cols):
                old = int(self.drops[c])
                self.drops[c] += self.speeds[c]
                new = int(self.drops[c])
                if new != old and 0 <= new < self.rows:
                    self.itemconfigure(self.cells[c][new], text=random.choice(self.CHARS), fill=self.levels[-1])
                    self.level[(c, new)] = self.MAX
                if self.drops[c] > self.rows + random.randint(0, 14):
                    self.drops[c] = random.uniform(-10, 0)
                    self.speeds[c] = random.uniform(0.25, 0.8)
            # случайная смена символов в «хвостах»
            for key in random.sample(list(self.level), min(6, len(self.level))):
                self.itemconfigure(self.cells[key[0]][key[1]], text=random.choice(self.CHARS))
            # сканирующая линия
            self.scan_y = (self.scan_y + 3) % (self.size[1] + 40)
            self.coords(self.scanline, 0, self.scan_y, self.size[0], self.scan_y + 2)
            # плавное заполнение индикатора диска
            used, total, _ = self.disk
            target = used / total if total else 0
            self.disk_shown += (target - self.disk_shown) * 0.08
            x0, x1 = self.bar_x
            color = GREEN if target < 0.8 else WARN if target < 0.92 else DANGER
            self.coords(self.disk_bar, x0, 80, x0 + (x1 - x0) * self.disk_shown, 93)
            self.itemconfigure(self.disk_bar, fill=color)
            self.itemconfigure(self.disk_pct, text=f"{self.disk_shown * 100:.1f}%", fill=color)
        self.after(70, self._tick)

    def _glitch(self):
        if self.size[0] and not self.glitch_dx:
            self.glitch_dx = random.choice((-5, -3, 3, 5))
            self.move("title", self.glitch_dx, 0)
            self.itemconfigure("title_main", fill="#eafff0")
            self.after(90, self._unglitch)
        self.after(random.randint(2500, 6000), self._glitch)

    def _unglitch(self):
        if self.glitch_dx:
            self.move("title", -self.glitch_dx, 0)
            self.glitch_dx = 0
        self.itemconfigure("title_main", fill=GREEN)


class StatCard(tk.Frame):
    """Карточка со счётчиком, который плавно «докручивается» до нового значения."""

    def __init__(self, master, caption, fmt):
        super().__init__(master, bg=PANEL, highlightthickness=1, highlightbackground=GREEN_DARK)
        tk.Frame(self, bg=GREEN, width=3).pack(side="left", fill="y")
        tk.Label(self, text=caption, bg=PANEL, fg=MUTED, font=(FONT, 9, "bold")).pack(anchor="w", padx=12,
                                                                                       pady=(8, 0))
        self.value = tk.Label(self, text="—", bg=PANEL, fg=GREEN, font=(FONT, 18, "bold"))
        self.value.pack(anchor="w", padx=12)
        self.sub = tk.Label(self, text=" ", bg=PANEL, fg=MUTED, font=(FONT, 9))
        self.sub.pack(anchor="w", padx=12, pady=(0, 8))
        self.fmt, self.cur, self.job = fmt, 0, None
        self.border = ColorAnimator(self)

    def count_to(self, target, sub=" "):
        if self.job:
            self.after_cancel(self.job)
        start = self.cur
        self.sub.configure(text=sub)

        def tick(i=1, n=40):
            self.cur = start + (target - start) * ease_out(i / n)
            self.value.configure(text=self.fmt(self.cur))
            self.job = self.after(18, tick, i + 1) if i < n else None
        tick()
        self.configure(highlightbackground=GREEN)
        self.border.to(steps=30, delay=30, highlightbackground=GREEN_DARK)


class TabBar(tk.Frame):
    """Вкладки с плавно «переезжающим» неоновым подчёркиванием."""

    def __init__(self, master, on_change):
        super().__init__(master, bg=BG)
        self.row = tk.Frame(self, bg=BG)
        self.row.pack(fill="x")
        self.line = tk.Canvas(self, height=3, bg=BG, highlightthickness=0)
        self.line.pack(fill="x")
        self.line.create_rectangle(0, 2, 10000, 3, fill=GREEN_DARK, width=0)
        self.ind = self.line.create_rectangle(0, 0, 0, 3, fill=GREEN, width=0)
        self.tabs, self.cur, self.on_change, self.pos, self.job = [], None, on_change, [0, 0], None

    def add(self, text):
        idx = len(self.tabs)
        lb = tk.Label(self.row, text=text, bg=BG, fg=MUTED, font=(FONT, 11, "bold"), padx=18, pady=8,
                      cursor="hand2")
        lb.pack(side="left")
        anim = ColorAnimator(lb)
        lb.bind("<Button-1>", lambda e: self.select(idx))
        lb.bind("<Enter>", lambda e: idx != self.cur and anim.to(fg=TEXT))
        lb.bind("<Leave>", lambda e: idx != self.cur and anim.to(fg=MUTED))
        self.tabs.append((lb, anim))

    def select(self, idx):
        if idx == self.cur:
            return
        if self.cur is not None:
            self.tabs[self.cur][1].to(fg=MUTED)
        self.cur = idx
        self.tabs[idx][1].to(fg=GREEN)
        self.update_idletasks()
        lb = self.tabs[idx][0]
        x, w = lb.winfo_x(), lb.winfo_width()
        if self.job:
            self.after_cancel(self.job)
        sx, sw = self.pos

        def tick(i=1, n=14):
            t = ease_out(i / n)
            cx, cw = sx + (x - sx) * t, sw + (w - sw) * t
            self.line.coords(self.ind, cx, 0, cx + cw, 3)
            self.pos = [cx, cw]
            self.job = self.after(16, tick, i + 1) if i < n else None
        tick()
        self.on_change(idx)


class ScannerBar(tk.Canvas):
    """Индикатор работы: светящийся «сканер» с хвостом, бегающий туда-обратно."""

    def __init__(self, master, width=220):
        super().__init__(master, width=width, height=12, bg=BG, highlightthickness=1,
                         highlightbackground=GREEN_DARK)
        self.w, self.n = width, 16
        segs = [self.create_rectangle(-10, 0, -10, 0, width=0, fill=blend(BG, GREEN, (1 - i / self.n) ** 1.6))
                for i in reversed(range(self.n))]
        self.segs = list(reversed(segs))  # индекс 0 — «голова», рисуется поверх хвоста
        self.running, self.x, self.dir = False, 0, 1

    def start(self):
        if not self.running:
            self.running = True
            self._tick()

    def stop(self):
        self.running = False

    def _tick(self):
        if not self.running:
            for s in self.segs:
                self.coords(s, -10, 0, -10, 0)
            return
        self.x += 6 * self.dir
        if self.x >= self.w:
            self.dir = -1
        elif self.x <= 0:
            self.dir = 1
        for i, s in enumerate(self.segs):
            sx = self.x - self.dir * i * 5
            self.coords(s, sx - 3, 2, sx + 3, 11)
        self.after(22, self._tick)


class StatusLine(tk.Label):
    """Строка статуса в стиле терминала: мигающий курсор и эффект печати."""

    def __init__(self, master):
        super().__init__(master, bg=BG, fg=TEXT, font=(FONT, 10), anchor="w")
        self.msg = self.shown = ""
        self.cursor, self.typing = True, None
        self._blink()

    def set(self, msg, typed=False):
        if self.typing:
            self.after_cancel(self.typing)
            self.typing = None
        self.msg = msg
        if typed:
            self.shown = ""
            self._type()
        else:
            self.shown = msg
            self._render()

    def _type(self):
        self.shown = self.msg[:len(self.shown) + 2]
        self._render()
        self.typing = self.after(14, self._type) if len(self.shown) < len(self.msg) else None

    def _render(self):
        self.configure(text="> " + self.shown + ("█" if self.cursor else " "))

    def _blink(self):
        self.cursor = not self.cursor
        self._render()
        self.after(500, self._blink)


class SortableTree(ttk.Treeview):
    """Treeview с сортировкой, «зеброй», подсветкой строки под курсором и вспышкой новых строк."""

    def __init__(self, master, columns, hint, **kw):
        super().__init__(master, columns=[c[0] for c in columns], show="headings", style="Neon.Treeview", **kw)
        self.raw = {}
        self._reverse = {}
        self.hovered = None
        for cid, title, width, anchor in columns:
            self.heading(cid, text=title, command=lambda c=cid: self.sort_by(c))
            self.column(cid, width=width, anchor=anchor, stretch=cid == columns[0][0])
        self.tag_configure("even", background=PANEL)
        self.tag_configure("odd", background=STRIPE)
        self.tag_configure("new", background=GREEN_DIM, foreground="#eafff0")
        self.tag_configure("hover", background=HOVER, foreground=GREEN)
        self.bind("<Motion>", self._hover)
        self.bind("<Leave>", lambda e: self._set_hover(None))
        self.hint = tk.Label(master, text=hint, bg=PANEL, fg=MUTED, font=(FONT, 11))
        self.after(50, self._show_hint)

    def _show_hint(self):
        self.hint.place(in_=self, relx=0.5, rely=0.5, anchor="center")

    def clear(self):
        self.delete(*self.get_children())
        self.raw.clear()
        self.hovered = None
        self._show_hint()

    def add_row(self, iid, values, raw, flash=False):
        self.hint.place_forget()
        self.insert("", "end", iid=iid, values=values, tags=("new",) if flash else (self._stripe(len(self.raw)),))
        self.raw[iid] = raw
        if flash:
            self.after(700, lambda: self.exists(iid) and self.item(iid, tags=(self._stripe(self.index(iid)),)))

    @staticmethod
    def _stripe(i):
        return "odd" if i % 2 else "even"

    def restripe(self):
        for i, iid in enumerate(self.get_children()):
            self.item(iid, tags=(self._stripe(i),))
        self.hovered = None

    def _hover(self, e):
        self._set_hover(self.identify_row(e.y) or None)

    def _set_hover(self, iid):
        if iid == self.hovered:
            return
        if self.hovered and self.exists(self.hovered):
            self.item(self.hovered, tags=[t for t in self.item(self.hovered, "tags") if t != "hover"])
        if iid:
            self.item(iid, tags=list(self.item(iid, "tags")) + ["hover"])
        self.hovered = iid

    def sort_by(self, col, reverse=None):
        rev = self._reverse.get(col, col.endswith("size")) if reverse is None else reverse

        def key(iid):
            v = self.raw.get(iid, {}).get(col)
            return v if v is not None else self.set(iid, col).lower()
        for i, iid in enumerate(sorted(self.get_children(), key=key, reverse=rev)):
            self.move(iid, "", i)
        self._reverse[col] = not rev
        self.restripe()


class NeonDialog(tk.Toplevel):
    def __init__(self, master, title, text, buttons, accent=GREEN):
        super().__init__(master)
        self.withdraw()
        self.title(title)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.result = None
        body = tk.Frame(self, bg=BG, highlightthickness=1, highlightbackground=accent)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="▌" + title.upper(), bg=BG, fg=accent, font=(FONT, 12, "bold"),
                 anchor="w").pack(fill="x", padx=20, pady=(16, 6))
        tk.Frame(body, bg=GREEN_DARK, height=1).pack(fill="x", padx=20)
        tk.Label(body, text=text, bg=BG, fg=TEXT, font=(FONT, 10), justify="left", wraplength=620,
                 anchor="w").pack(fill="x", padx=20, pady=16)
        row = tk.Frame(body, bg=BG)
        row.pack(anchor="e", padx=20, pady=(0, 16))
        for label, value in buttons:
            NeonButton(row, label, lambda v=value: self.close(v),
                       accent=accent if value else MUTED, bg=BG).pack(side="left", padx=(8, 0))
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", lambda: self.close(None))
        self.bind("<Escape>", lambda e: self.close(None))
        self.bind("<Return>", lambda e: self.close(buttons[0][1]))
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = master.winfo_rootx() + (master.winfo_width() - w) // 2
        y = master.winfo_rooty() + (master.winfo_height() - h) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.attributes("-alpha", 0.0)
        self.deiconify()
        dark_title_bar(self)
        fade_in(self, step=0.12)
        self.grab_set()
        self.focus_set()

    def close(self, value):
        self.result = value
        self.grab_release()
        self.destroy()


class LogWindow(tk.Toplevel):
    """Окно-терминал с журналом выполненных команд."""

    def __init__(self, master, title):
        super().__init__(master)
        self.title(title)
        self.configure(bg=BG)
        self.geometry(f"860x480+{master.winfo_rootx() + 160}+{master.winfo_rooty() + 140}")
        tk.Label(self, text="▌" + title.upper(), bg=BG, fg=GREEN, font=(FONT, 12, "bold"),
                 anchor="w").pack(fill="x", padx=16, pady=(12, 6))
        box = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=GREEN_DIM)
        box.pack(fill="both", expand=True, padx=16)
        self.text = tk.Text(box, bg=PANEL, fg=TEXT, font=(FONT, 10), relief="flat", wrap="word",
                            insertbackground=GREEN, padx=10, pady=8)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.text.yview, style="Neon.Vertical.TScrollbar")
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("ok", foreground=GREEN)
        self.text.tag_configure("err", foreground=DANGER)
        self.text.tag_configure("head", foreground=WARN)
        self.close_btn = NeonButton(self, "ЗАКРЫТЬ", self.destroy, bg=BG)
        self.close_btn.pack(anchor="e", padx=16, pady=12)
        self.transient(master)
        dark_title_bar(self)

    def write(self, line, tag="ok"):
        if self.winfo_exists():
            self.text.insert("end", line + "\n", tag)
            self.text.see("end")


# ---------- Главное окно ----------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()
        self.title("Disk Scanner C:")
        self.geometry("1200x780")
        self.minsize(980, 620)
        self.configure(bg=BG)
        try:
            self.iconbitmap(default=resource_path("icon.ico"))
        except tk.TclError:
            pass
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self._toast = None
        self.setup_style()

        self.header = MatrixHeader(self)
        self.header.pack(fill="x")
        if not ctypes.windll.shell32.IsUserAnAdmin():
            NeonButton(self.header, "⚡ Перезапустить от администратора", self.relaunch_as_admin,
                       accent=WARN, bg=BG).place(relx=1.0, x=-28, y=14, anchor="ne")

        stats = tk.Frame(self, bg=BG)
        stats.pack(fill="x", padx=16, pady=(14, 4))
        self.card_apps = StatCard(stats, "УСТАНОВЛЕНО ПРОГРАММ", fmt_int)
        self.card_temp = StatCard(stats, "ВРЕМЕННЫЕ ФАЙЛЫ", human_size)
        self.card_big = StatCard(stats, "БОЛЬШИЕ ФАЙЛЫ", human_size)
        self.card_free = StatCard(stats, "СВОБОДНО НА C:", human_size)
        for i, card in enumerate((self.card_apps, self.card_temp, self.card_big, self.card_free)):
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 10, 0))
            stats.columnconfigure(i, weight=1, uniform="cards")

        self.tabbar = TabBar(self, self.show_tab)
        self.tabbar.pack(fill="x", padx=16, pady=(10, 0))
        self.content = tk.Frame(self, bg=BG)
        self.content.pack(fill="both", expand=True, padx=16, pady=(10, 0))

        bottom = tk.Frame(self, bg=BG)
        bottom.pack(fill="x", padx=16, pady=10)
        self.scanner = ScannerBar(bottom)
        self.scanner.pack(side="right")
        self.stop_btn = NeonButton(bottom, "■ СТОП", self.stop_event.set, accent=DANGER, bg=BG)
        self.stop_btn.pack(side="right", padx=10)
        self.stop_btn.set_enabled(False)
        self.status = StatusLine(bottom)
        self.status.pack(side="left", fill="x", expand=True)

        self.pages = [self.build_apps_tab(), self.build_temp_tab(), self.build_big_tab()]
        for title in ("ПРИЛОЖЕНИЯ", "ВРЕМЕННЫЕ ФАЙЛЫ", "БОЛЬШИЕ ФАЙЛЫ > 500 МБ"):
            self.tabbar.add(title)

        self.update_disk_info()
        self.after(100, self.poll_queue)
        self.attributes("-alpha", 0.0)
        self.deiconify()
        dark_title_bar(self)
        fade_in(self)
        self.after(120, lambda: self.tabbar.select(0))
        self.after(300, lambda: self.status.set("Система готова. Выберите раздел и нажмите «Сканировать».",
                                                typed=True))

    def setup_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Neon.Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=28, font=(FONT, 10), borderwidth=0, relief="flat")
        style.map("Neon.Treeview", background=[("selected", GREEN_DIM)], foreground=[("selected", "#eafff0")])
        style.layout("Neon.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.configure("Neon.Treeview.Heading", background=PANEL2, foreground=GREEN, font=(FONT, 10, "bold"),
                        relief="flat", borderwidth=0, padding=(8, 7))
        style.map("Neon.Treeview.Heading", background=[("active", GREEN_DARK)], foreground=[("active", GREEN)])
        style.configure("Neon.Vertical.TScrollbar", troughcolor=BG, background=GREEN_DARK, bordercolor=BG,
                        arrowcolor=GREEN, lightcolor=GREEN_DARK, darkcolor=GREEN_DARK, gripcount=0, arrowsize=14)
        style.map("Neon.Vertical.TScrollbar", background=[("active", GREEN_DIM)])

    # ----- общие -----

    def page_frame(self):
        return tk.Frame(self.content, bg=BG)

    def toolbar(self, page):
        bar = tk.Frame(page, bg=BG)
        bar.pack(fill="x", pady=(0, 10))
        return bar

    def tree_box(self, page):
        box = tk.Frame(page, bg=PANEL, highlightthickness=1, highlightbackground=GREEN_DIM)
        box.pack(fill="both", expand=True)
        return box

    @staticmethod
    def attach_scroll(box, tree):
        sb = ttk.Scrollbar(box, orient="vertical", command=tree.yview, style="Neon.Vertical.TScrollbar")
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)

    def show_tab(self, idx):
        for p in self.pages:
            p.place_forget()
        page = self.pages[idx]

        def slide(i=1, n=12):
            page.place(in_=self.content, x=round(48 * (1 - ease_out(i / n))), y=0, relwidth=1, relheight=1)
            if i < n:
                self.after(16, slide, i + 1)
        slide()

    def ask(self, title, text, accent=GREEN):
        d = NeonDialog(self, title, text, [("ДА", True), ("ОТМЕНА", False)], accent)
        self.wait_window(d)
        return d.result is True

    def info(self, title, text, accent=GREEN):
        d = NeonDialog(self, title, text, [("OK", True)], accent)
        self.wait_window(d)

    def toast(self, text):
        if self._toast:
            self._toast.destroy()
        f = tk.Frame(self, bg=PANEL2, highlightthickness=1, highlightbackground=GREEN)
        tk.Label(f, text="✔  " + text, bg=PANEL2, fg=GREEN, font=(FONT, 10, "bold"), padx=16, pady=10,
                 wraplength=420, justify="left").pack()
        self._toast = f
        f.update_idletasks()
        w = f.winfo_reqwidth()

        def slide(start, end, then=None, i=1, n=16):
            if not f.winfo_exists():
                return
            f.place(relx=1.0, rely=1.0, x=round(start + (end - start) * ease_out(i / n)), y=-64, anchor="se")
            if i < n:
                self.after(16, slide, start, end, then, i + 1)
            elif then:
                then()
        slide(w + 20, -20, lambda: self.after(3500, lambda: slide(-20, w + 20, f.destroy)))

    def relaunch_as_admin(self):
        if getattr(sys, "frozen", False):
            exe, params = sys.executable, ""
        else:
            exe = sys.executable.replace("python.exe", "pythonw.exe")
            params = f'"{os.path.abspath(__file__)}"'
        if ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1) > 32:
            self.destroy()

    def update_disk_info(self):
        free, total = ctypes.c_ulonglong(), ctypes.c_ulonglong()
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(DRIVE, None, ctypes.byref(total), ctypes.byref(free))
        self.header.set_disk(total.value - free.value, total.value, free.value)
        self.card_free.count_to(free.value, f"из {human_size(total.value)}")

    def start_task(self, target, *args):
        if self.worker and self.worker.is_alive():
            self.info("Подождите", "Уже идёт сканирование. Дождитесь окончания или нажмите «СТОП».", WARN)
            return
        self.stop_event.clear()
        self.scanner.start()
        self.stop_btn.set_enabled(True)
        self.worker = threading.Thread(target=self._run, args=(target, *args), daemon=True)
        self.worker.start()

    def _run(self, target, *args):
        try:
            target(*args)
        except Exception as ex:  # чтобы ошибка не "подвесила" интерфейс
            self.q.put(("error", str(ex)))
        finally:
            self.q.put(("done", None))

    def poll_queue(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "status":
                    self.status.set(data)
                elif kind == "call":
                    data()
                elif kind == "error":
                    self.info("Ошибка", data, DANGER)
                elif kind == "done":
                    self.scanner.stop()
                    self.stop_btn.set_enabled(False)
                    self.update_disk_info()
        except queue.Empty:
            pass
        self.after(80, self.poll_queue)

    def ui(self, fn):
        self.q.put(("call", fn))

    def set_status(self, text):
        self.q.put(("status", text))

    # ----- вкладка "Приложения" -----

    def build_apps_tab(self):
        page = self.page_frame()
        bar = self.toolbar(page)
        NeonButton(bar, "▶ СКАНИРОВАТЬ", lambda: self.start_task(self.scan_apps), bg=BG).pack(side="left")
        NeonButton(bar, "📂 Открыть папку", self.open_app_folder, bg=BG).pack(side="left", padx=8)
        NeonButton(bar, "✖ Деинсталлятор", self.uninstall_app, accent=WARN, bg=BG).pack(side="left")
        NeonButton(bar, "☠ Полное удаление", self.force_uninstall_app, accent=DANGER, bg=BG).pack(side="left",
                                                                                                 padx=8)
        self.app_filter = tk.StringVar()
        self.app_filter.trace_add("write", lambda *_: self.fill_apps())
        entry = tk.Entry(bar, textvariable=self.app_filter, width=28, bg=PANEL2, fg=GREEN, font=(FONT, 10),
                         insertbackground=GREEN, relief="flat", highlightthickness=1,
                         highlightbackground=GREEN_DARK, highlightcolor=GREEN)
        entry.pack(side="right", ipady=5)
        tk.Label(bar, text="ПОИСК ›", bg=BG, fg=MUTED, font=(FONT, 10, "bold")).pack(side="right", padx=6)

        box = self.tree_box(page)
        self.apps_tree = SortableTree(box, [
            ("name", "НАЗВАНИЕ", 380, "w"),
            ("publisher", "ИЗДАТЕЛЬ", 220, "w"),
            ("version", "ВЕРСИЯ", 120, "w"),
            ("size", "РАЗМЕР", 100, "e"),
            ("date", "УСТАНОВЛЕНО", 110, "center"),
        ], "Нажмите «▶ СКАНИРОВАТЬ», чтобы получить список программ", selectmode="browse")
        self.attach_scroll(box, self.apps_tree)
        self.apps = []
        return page

    def scan_apps(self):
        self.set_status("Чтение списка установленных программ из реестра…")
        apps = get_installed_apps()

        def done():
            self.apps = apps
            self.fill_apps()
            total = sum(a["size"] for a in apps)
            self.card_apps.count_to(len(apps), f"≈ {human_size(total)} по данным установщиков")
            self.status.set(f"Найдено программ: {len(apps)}", typed=True)
            self.toast(f"Найдено программ: {len(apps)}")
        self.ui(done)

    def fill_apps(self):
        t = self.apps_tree
        t.clear()
        flt = self.app_filter.get().lower()
        for i, a in enumerate(sorted(self.apps, key=lambda a: -a["size"])):
            if flt and flt not in a["name"].lower() and flt not in str(a["publisher"]).lower():
                continue
            d, m, y = a["date"].split(".") if a["date"].count(".") == 2 else ("", "", "")
            t.add_row(str(i), (a["name"], a["publisher"], a["version"],
                               human_size(a["size"]) if a["size"] else "—", a["date"]),
                      {"size": a["size"], "date": y + m + d, "app": a})

    def selected_app(self):
        sel = self.apps_tree.selection()
        if not sel:
            self.info("Нет выбора", "Выберите программу в списке.")
            return None
        return self.apps_tree.raw[sel[0]]["app"]

    def open_app_folder(self):
        app = self.selected_app()
        if not app:
            return
        loc = str(app["location"]).strip('"')
        if loc and os.path.isdir(loc):
            os.startfile(loc)
        else:
            self.info("Нет данных", "Установщик не сообщил папку этой программы.", WARN)

    def uninstall_app(self):
        app = self.selected_app()
        if not app:
            return
        cmd = app["uninstall"]
        if not cmd:
            self.info("Невозможно", "Для этой программы не указан деинсталлятор.", WARN)
            return
        if self.ask("Удаление программы", f"Запустить стандартный деинсталлятор программы\n\n{app['name']}?",
                    WARN):
            try:
                subprocess.Popen(cmd, shell=True)
            except OSError as ex:
                self.info("Ошибка", str(ex), DANGER)

    def force_uninstall_app(self):
        if self.worker and self.worker.is_alive():
            self.info("Подождите", "Сейчас выполняется другая операция. Дождитесь её окончания.", WARN)
            return
        app = self.selected_app()
        if not app:
            return
        plan = plan_force_removal(app, self.apps)
        if needs_admin(plan) and not ctypes.windll.shell32.IsUserAnAdmin():
            if self.ask("Нужны права администратора",
                        "Для полного удаления этой программы нужны права администратора.\n\n"
                        "Перезапустить сканер от имени администратора?", WARN):
                self.relaunch_as_admin()
            return

        def section(title, items, limit=8):
            if not items:
                return ""
            lines = "\n".join("   " + str(i) for i in items[:limit])
            more = f"\n   … и ещё {len(items) - limit}" if len(items) > limit else ""
            return f"\n{title}:\n{lines}{more}\n"
        text = (f"Программа будет удалена БЕЗ деинсталлятора, командами cmd.\n"
                f"Это необратимо — файлы удаляются минуя корзину.\n"
                + section("Папка программы (rd /s /q)", [plan["folder"]] if plan["folder"] else [])
                + section("Остатки в AppData / ProgramData / меню Пуск", plan["folders"])
                + section("Службы (sc delete)", plan["services"])
                + section("Ярлыки (del)", plan["shortcuts"])
                + section("Ключи реестра (reg delete)", [k for k, _ in plan["reg"]])
                + ("\n" + "\n".join("⚠ " + n for n in plan["notes"]) if plan["notes"] else "")
                + "\n\nЗапущенные процессы программы будут завершены (taskkill).")
        if not self.ask(f"Полное удаление: {app['name']}", text, DANGER):
            return
        log = LogWindow(self, f"Полное удаление: {app['name']}")
        log.write(f"> Удаление «{app['name']}»", "head")

        def logger(line, ok):
            self.ui(lambda: log.write(line, "ok" if ok else "err"))

        def work():
            self.set_status(f"Полное удаление: {app['name']}…")
            force_remove(plan, logger)

            def done():
                log.write("> Готово.", "head")
                if not reg_exists(app["reg_key"], app["reg_view"]):
                    self.apps = [a for a in self.apps if a is not app]
                    self.fill_apps()
                    self.card_apps.count_to(len(self.apps), self.card_apps.sub.cget("text"))
                    self.toast(f"Программа «{app['name']}» удалена")
                self.status.set(f"Полное удаление «{app['name']}» завершено", typed=True)
            self.ui(done)
        self.start_task(work)

    # ----- вкладка "Временные файлы" -----

    def build_temp_tab(self):
        page = self.page_frame()
        bar = self.toolbar(page)
        NeonButton(bar, "▶ СКАНИРОВАТЬ", lambda: self.start_task(self.scan_temp), bg=BG).pack(side="left")
        NeonButton(bar, "📂 Открыть папку", self.open_temp_folder, bg=BG).pack(side="left", padx=8)
        NeonButton(bar, "🧹 Очистить выбранное", self.clean_temp, accent=WARN, bg=BG).pack(side="left")
        tk.Label(bar, text="Ctrl/Shift + клик — несколько категорий · файлы моложе 1 часа и занятые не удаляются",
                 bg=BG, fg=MUTED, font=(FONT, 9)).pack(side="left", padx=14)

        box = self.tree_box(page)
        self.temp_tree = SortableTree(box, [
            ("name", "КАТЕГОРИЯ", 330, "w"),
            ("path", "ПАПКА", 450, "w"),
            ("count", "ФАЙЛОВ", 90, "e"),
            ("size", "РАЗМЕР", 110, "e"),
        ], "Нажмите «▶ СКАНИРОВАТЬ», чтобы найти временные файлы", selectmode="extended")
        self.attach_scroll(box, self.temp_tree)
        return page

    def scan_temp(self):
        self.ui(self.temp_tree.clear)
        total = 0
        for idx, (name, path) in enumerate(temp_locations()):
            if self.stop_event.is_set():
                break
            self.set_status(f"Сканирование: {path}")
            size = count = 0
            for e in iter_files(path, self.stop_event):
                if not temp_filter(name, e):
                    continue
                try:
                    size += e.stat(follow_symlinks=False).st_size
                    count += 1
                except OSError:
                    pass
            total += size
            if count:
                self.ui(lambda iid=str(idx), name=name, path=path, count=count, size=size: self.temp_tree.add_row(
                    iid, (name, path, fmt_int(count), human_size(size)),
                    {"size": size, "count": count, "name": name, "path": path}, flash=True))

        def done():
            self.temp_tree.sort_by("size", reverse=True)
            self.temp_tree.selection_set(self.temp_tree.get_children())
            self.card_temp.count_to(total, f"{len(self.temp_tree.get_children())} категорий")
            self.status.set(f"Временные файлы: всего {human_size(total)}", typed=True)
            self.toast(f"Найдено временных файлов на {human_size(total)}")
        self.ui(done)

    def open_temp_folder(self):
        sel = self.temp_tree.selection()
        if sel:
            os.startfile(self.temp_tree.raw[sel[0]]["path"])

    def clean_temp(self):
        sel = self.temp_tree.selection()
        if not sel:
            self.info("Нет выбора", "Выберите категории для очистки.")
            return
        items = [self.temp_tree.raw[i] for i in sel]
        total = sum(i["size"] for i in items)
        if not self.ask("Очистка",
                        f"Удалить временные файлы ({len(items)} категорий, до {human_size(total)})?\n\n"
                        "Файлы удаляются безвозвратно (минуя корзину). Рекомендуется закрыть браузеры.", WARN):
            return
        self.start_task(self._clean_temp, items)

    def _clean_temp(self, items):
        freed = deleted = skipped = 0
        now = time.time()
        for item in items:
            self.set_status(f"Очистка: {item['path']}")
            for e in list(iter_files(item["path"], self.stop_event)):
                if self.stop_event.is_set():
                    break
                if not temp_filter(item["name"], e):
                    continue
                try:
                    st = e.stat(follow_symlinks=False)
                    if now - st.st_mtime < TEMP_MIN_AGE_SEC:
                        skipped += 1
                        continue
                    if st.st_file_attributes & stat.FILE_ATTRIBUTE_READONLY:
                        os.chmod(e.path, stat.S_IWRITE)
                    os.remove(e.path)
                    freed += st.st_size
                    deleted += 1
                except OSError:
                    skipped += 1
            self._remove_empty_dirs(item["path"])
        msg = (f"Удалено файлов: {fmt_int(deleted)}, освобождено {human_size(freed)}.\n"
               f"Пропущено (заняты или свежие): {fmt_int(skipped)}")
        self.ui(lambda: self.info("Очистка завершена", msg))
        self.scan_temp()

    @staticmethod
    def _remove_empty_dirs(root):
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            if dirpath == root:
                continue
            try:
                os.rmdir(dirpath)  # удалит только пустые папки
            except OSError:
                pass

    # ----- вкладка "Большие файлы" -----

    def build_big_tab(self):
        page = self.page_frame()
        bar = self.toolbar(page)
        NeonButton(bar, "▶ СКАНИРОВАТЬ", lambda: self.start_task(self.scan_big), bg=BG).pack(side="left")
        NeonButton(bar, "📂 Показать в проводнике", self.show_big_in_explorer, bg=BG).pack(side="left", padx=8)
        NeonButton(bar, "🗑 В корзину", self.recycle_big, accent=WARN, bg=BG).pack(side="left")
        self.include_programs = tk.BooleanVar(value=False)
        NeonCheck(bar, "искать также в Program Files и ProgramData", self.include_programs).pack(side="left",
                                                                                                padx=16)
        tk.Label(page, bg=BG, fg=MUTED, font=(FONT, 9), anchor="w",
                 text="Папка Windows, корзина, точки восстановления, файлы подкачки/гибернации и файлы "
                      "с атрибутом «системный» не сканируются.").pack(side="bottom", fill="x", pady=(8, 0))

        box = self.tree_box(page)
        self.big_tree = SortableTree(box, [
            ("path", "ФАЙЛ", 680, "w"),
            ("size", "РАЗМЕР", 110, "e"),
            ("modified", "ИЗМЕНЁН", 150, "center"),
        ], "Нажмите «▶ СКАНИРОВАТЬ», чтобы найти файлы больше 500 МБ", selectmode="extended")
        self.attach_scroll(box, self.big_tree)
        return page

    def scan_big(self):
        self.ui(self.big_tree.clear)
        skip = set(SYSTEM_DIRS)
        if not self.include_programs.get():
            skip |= PROGRAM_DIRS
        found = []
        stack = [DRIVE]
        dirs_scanned = 0
        last_status = 0
        while stack and not self.stop_event.is_set():
            d = stack.pop()
            dirs_scanned += 1
            if time.time() - last_status > 0.15:
                self.set_status(f"[{fmt_int(dirs_scanned)} папок · найдено {len(found)}] {d}")
                last_status = time.time()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                if os.path.normcase(e.path) in skip or is_reparse_point(e):
                                    continue
                                stack.append(e.path)
                            elif e.is_file(follow_symlinks=False):
                                st = e.stat(follow_symlinks=False)
                                if st.st_size < BIG_FILE_LIMIT:
                                    continue
                                if st.st_file_attributes & FILE_ATTRIBUTE_SYSTEM or e.name.lower() in SYSTEM_FILE_NAMES:
                                    continue
                                found.append((e.path, st.st_size, st.st_mtime))
                                self.ui(lambda p=e.path, s=st.st_size, m=st.st_mtime: self._add_big(p, s, m))
                        except OSError:
                            continue
            except OSError:
                continue
        total = sum(f[1] for f in found)
        stopped = " (остановлено)" if self.stop_event.is_set() else ""

        def done():
            self.big_tree.sort_by("size", reverse=True)
            self.card_big.count_to(total, f"{len(found)} файлов")
            self.status.set(f"Больших файлов: {len(found)}, общий объём {human_size(total)}{stopped}", typed=True)
            self.toast(f"Найдено больших файлов: {len(found)} ({human_size(total)})")
        self.ui(done)

    def _add_big(self, path, size, mtime):
        if self.big_tree.exists(path):
            return
        self.big_tree.add_row(path, (path, human_size(size), time.strftime("%d.%m.%Y %H:%M", time.localtime(mtime))),
                              {"size": size, "modified": mtime}, flash=True)

    def show_big_in_explorer(self):
        sel = self.big_tree.selection()
        if sel:
            subprocess.Popen(f'explorer /select,"{sel[0]}"')

    def recycle_big(self):
        sel = self.big_tree.selection()
        if not sel:
            self.info("Нет выбора", "Выберите файлы в списке.")
            return
        total = sum(self.big_tree.raw[i]["size"] for i in sel)
        names = "\n".join(sel[:10]) + ("\n…" if len(sel) > 10 else "")
        if not self.ask("В корзину", f"Переместить в корзину {len(sel)} файл(ов), {human_size(total)}?\n\n{names}\n\n"
                                     "Место освободится после очистки корзины.", WARN):
            return
        failed = []
        for path in sel:
            if move_to_recycle_bin(path) and not os.path.exists(path):
                self.big_tree.delete(path)
                self.big_tree.raw.pop(path, None)
            else:
                failed.append(path)
        self.big_tree.restripe()
        if failed:
            self.info("Не удалось", "Не удалось переместить (файл занят или нет прав):\n\n" + "\n".join(failed),
                      DANGER)
        else:
            self.toast(f"Перемещено в корзину: {len(sel)} файл(ов)")
        self.update_disk_info()


if __name__ == "__main__":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # чёткий шрифт на экранах с масштабированием
    except Exception:
        pass
    App().mainloop()
