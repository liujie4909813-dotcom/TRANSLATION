# -*- coding: utf-8 -*-
"""
zh2en — 中文输入即时转英文（Windows 全局快捷键工具）

用法：
  1) 在任意软件里框选中文 -> 按 Alt+[ -> 选中的中文被就地替换成英文
  2) 没框选任何东西 -> 按 Alt+[ -> 弹出输入框，敲中文回车即出英文（Ctrl+Enter 直接贴回原窗口）
  3) 复制了中文 -> 按 Alt+] -> 剪贴板里的中文变成英文
  4) Ctrl+Alt+Q 退出
  5) 不用快捷键：双击「翻译台.bat」，右下角常驻小窗口，打字就出英文

  --tr "文字"   命令行翻译
  --check      环境自检（含快捷键能否注册）
  --keys       按键诊断
  --panel      翻译台（无快捷键）
"""

import ctypes
import html as html_mod
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.parse
import urllib.request

try:
    import keyboard
    import pyperclip
except ImportError:
    print("[错误] 缺少依赖，请先运行「安装依赖.bat」，或执行：")
    print("       python -m pip install keyboard pyperclip")
    sys.exit(1)

try:
    import tkinter as tk
    HAS_TK = True
except Exception:
    tk = None
    HAS_TK = False

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(APP_DIR, "config.json")

try:  # 让控制台中文不乱码、日志即时可见
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

DEFAULT_CFG = {
    "hotkey_translate": "ctrl+alt+t",
    "hotkey_translate_reverse": "alt+;",
    "hotkey_clipboard": "ctrl+alt+y",
    "hotkey_quit": "ctrl+alt+q",
    "providers": ["mymemory", "google", "youdao"],
    "insert_method": "auto",
    "restore_clipboard": True,
    "paste_delay": 0.35,
    "http_timeout": 10,
}

CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")

# 翻译方向：(源语言, 目标语言)
DIRECTIONS = {
    "zh2en": ("zh-CN", "en"),
    "en2zh": ("en", "zh-CN"),
}

user32 = ctypes.windll.user32


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def load_cfg():
    cfg = dict(DEFAULT_CFG)
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f) or {})
        except Exception as e:
            print(f"[警告] config.json 读取失败，使用默认配置：{e}")
    return cfg


CFG = load_cfg()


# --------------------------------------------------------------------------
# 剪贴板 / 窗口
# --------------------------------------------------------------------------
def clip_get():
    for _ in range(4):
        try:
            return pyperclip.paste() or ""
        except Exception:
            time.sleep(0.03)
    return ""


def clip_set(text):
    for _ in range(4):
        try:
            pyperclip.copy(text)
            return True
        except Exception:
            time.sleep(0.03)
    return False


def fg_window():
    return user32.GetForegroundWindow()


def activate_window(hwnd):
    if not hwnd:
        return
    try:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.08)
    except Exception:
        pass


def wait_modifiers_release(timeout=1.5):
    """等用户松开 ctrl/alt，否则模拟的 ctrl+c 会变成 ctrl+alt+c。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        held = False
        for k in ("ctrl", "alt", "shift", "windows"):
            try:
                if keyboard.is_pressed(k):
                    held = True
                    break
            except Exception:
                continue
        if not held:
            return True
        time.sleep(0.02)
    return False


def log(msg):
    ts = time.strftime("%H:%M:%S")
    try:
        print(f"[{ts}] {msg}")
    except Exception:
        pass


# --------------------------------------------------------------------------
# 自己的热键匹配器
#
# 不用 keyboard.add_hotkey：它按「按键名」精确匹配整个组合串，中文输入法
# 活跃时经常匹配不上，且单字符键（[ ]）在它的解析里容易出岔子。
# 这里改成自己维护按键状态，按「修饰键集合 + 主键」判断，容错更高。
# --------------------------------------------------------------------------
MOD_ALIAS = {
    "left alt": "alt", "right alt": "alt", "alt gr": "alt", "altgr": "alt",
    "left ctrl": "ctrl", "right ctrl": "ctrl",
    "left shift": "shift", "right shift": "shift",
    "windows": "windows", "left windows": "windows", "right windows": "windows",
    "left menu": "alt", "right menu": "alt",
}


def _canon(name):
    n = (name or "").lower()
    return MOD_ALIAS.get(n, n)


class HotkeyMatcher:
    def __init__(self, bindings):
        """bindings: [(combo, callback), ...]，combo 形如 'alt+[' 或 'ctrl+alt+q'。"""
        self.bindings = []
        for combo, fn in bindings:
            parts = [_canon(p.strip()) for p in combo.split("+") if p.strip()]
            if not parts:
                continue
            self.bindings.append((frozenset(parts[:-1]), parts[-1], fn, combo))
        self.pressed = set()
        self._last = {}
        self.seen = []          # 调试用：最近收到的按键

    def on_event(self, e):
        name = _canon(e.name)
        if not name:
            return
        if e.event_type == "down":
            self.pressed.add(name)
            self.seen.append(name)
            del self.seen[:-24]
            for mods, main, fn, combo in self.bindings:
                if name != main or not mods <= self.pressed:
                    continue
                # 以物理状态为准：切窗口时容易漏掉抬起事件，留着过期状态会误触发
                if mods:
                    try:
                        if not all(keyboard.is_pressed(m) for m in mods):
                            self.pressed -= set(mods)
                            continue
                    except Exception:
                        pass
                now = time.time()
                if now - self._last.get(combo, 0) < 0.8:   # 长按防连发
                    return
                self._last[combo] = now
                log(f"命中 {combo}")
                threading.Thread(target=self._run, args=(fn, combo), daemon=True).start()
                return
        else:
            self.pressed.discard(name)

    def _run(self, fn, combo):
        try:
            fn()
        except Exception as ex:
            log(f"[{combo}] 执行出错：{type(ex).__name__} {ex}")


# ---------------- 系统级热键（RegisterHotKey） ----------------
# 为什么不用 keyboard.hook：低级键盘钩子收不收得到事件取决于权限、输入法、安全软件，
# 一旦收不到就完全没有反馈。RegisterHotKey 由系统分发，注册成功/失败都有明确结果，
# 而且按键会被系统吃掉，不会再漏一个 '[' 到输入框里。
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x0008, 0x4000
WM_HOTKEY = 0x0312
MOD_NAMES = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
             "shift": MOD_SHIFT, "win": MOD_WIN, "windows": MOD_WIN}
NAMED_VK = {"space": 0x20, "tab": 0x09, "enter": 0x0D, "esc": 0x1B, "`": 0xC0}

_u32 = ctypes.WinDLL("user32", use_last_error=True)
try:
    _u32.VkKeyScanW.argtypes = [ctypes.c_wchar]
    _u32.VkKeyScanW.restype = ctypes.c_short
except Exception:
    pass


def combo_to_hotkey(combo):
    """'alt+[' → (mods, vk)；无法解析时返回 (None, None)。"""
    parts = [p.strip().lower() for p in (combo or "").split("+") if p.strip()]
    if not parts:
        return None, None
    mods = 0
    for p in parts[:-1]:
        if p not in MOD_NAMES:
            return None, None
        mods |= MOD_NAMES[p]
    key = parts[-1]
    if len(key) == 1:
        vk_scan = _u32.VkKeyScanW(key)
        if vk_scan in (-1, 0xFFFF):
            return None, None
        vk = vk_scan & 0xFF
        if (vk_scan >> 8) & 1:          # 这个字符在当前布局上要按着 Shift 才出
            mods |= MOD_SHIFT
        return mods, vk
    if key in NAMED_VK:
        return mods, NAMED_VK[key]
    if len(key) >= 2 and key[0] == "f" and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return mods, 0x6F + int(key[1:])
    return None, None


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MSG(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                ("time", ctypes.c_ulong), ("pt", _POINT)]


def hotkey_test(combo, hold_ms=0):
    """只验证某个组合键能不能被系统注册（注册完立刻注销），返回 (ok, 错误码)。"""
    mods, vk = combo_to_hotkey(combo)
    if vk is None:
        return False, -1
    ok = _u32.RegisterHotKey(None, 0xBEEF, mods | MOD_NOREPEAT, vk)
    err = 0 if ok else ctypes.get_last_error()
    if ok:
        _u32.UnregisterHotKey(None, 0xBEEF)
    return bool(ok), err


class HotkeyListener:
    """系统级热键监听。注册和消息循环必须在同一个线程里。"""

    def __init__(self, bindings):
        self.bindings = bindings
        self.registered = []        # [(id, combo, fn)]
        self.failed = []            # [(combo, 错误码)]
        self.thread_id = None
        self.ready = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        self.ready.wait(4)
        return self

    def _run(self):
        self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        for i, (combo, fn) in enumerate(self.bindings, 1):
            mods, vk = combo_to_hotkey(combo)
            if vk is None:
                self.failed.append((combo, -1))
                continue
            if _u32.RegisterHotKey(None, i, mods | MOD_NOREPEAT, vk):
                self.registered.append((i, combo, fn))
            else:
                self.failed.append((combo, ctypes.get_last_error()))
        self.ready.set()

        fns = {i: (combo, fn) for i, combo, fn in self.registered}
        msg = _MSG()
        while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message != WM_HOTKEY:
                continue
            hit = fns.get(int(msg.wParam))
            if not hit:
                continue
            combo, fn = hit
            log(f"命中 {combo}")
            threading.Thread(target=self._run_fn, args=(fn, combo), daemon=True).start()

        # UnregisterHotKey 必须在注册它的线程里调用，所以在这儿收尾
        for i, _c, _f in self.registered:
            try:
                _u32.UnregisterHotKey(None, i)
            except Exception:
                pass
        self.registered.clear()

    def _run_fn(self, fn, combo):
        try:
            fn()
        except Exception as ex:
            log(f"[{combo}] 执行出错：{type(ex).__name__} {ex}")

    def stop(self):
        if self.thread_id:
            _u32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)   # WM_QUIT


def key_probe(seconds=40):
    """按键诊断：把你按的每个键原样打印出来，用来确认系统到底有没有收到。"""
    print("=" * 58)
    print("  按键诊断（%d 秒后自动结束，Ctrl+C 可提前退出）" % seconds)
    print("  现在去按 Alt+[ 和 Alt+] 各两下，看下面有没有输出。")
    print("  把这些输出整段发回来，就能定位是「键没收到」还是「匹配没命中」。")
    print("=" * 58)

    def cb(e):
        print(f"  {e.event_type:<5} name={e.name!r:<16} scan={e.scan_code:<6} "
              f"alt={keyboard.is_pressed('alt')} ctrl={keyboard.is_pressed('ctrl')}")

    keyboard.hook(cb)
    t0 = time.time()
    try:
        while time.time() - t0 < seconds:
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    keyboard.unhook_all()
    print("-" * 58)
    print("  诊断结束。")


def _send_combo(mod, key, hold=0.05):
    """按住 mod 再敲 key。比 keyboard.send 的瞬时按下更稳，多数程序认这个。"""
    try:
        keyboard.press(mod)
        time.sleep(0.02)
        keyboard.press(key)
        time.sleep(hold)
        keyboard.release(key)
        time.sleep(0.02)
        keyboard.release(mod)
    except Exception:
        try:
            keyboard.send(f"{mod}+{key}")
        except Exception:
            pass


WM_COPY, WM_PASTE = 0x0301, 0x0302
SMTO_ABORTIFHUNG, SMTO_BLOCK = 0x0002, 0x0001


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint),
                ("flags", ctypes.c_uint),
                ("hwndActive", ctypes.c_void_p),
                ("hwndFocus", ctypes.c_void_p),
                ("hwndCapture", ctypes.c_void_p),
                ("hwndMenuOwner", ctypes.c_void_p),
                ("hwndMoveSize", ctypes.c_void_p),
                ("hwndCaret", ctypes.c_void_p),
                ("rcCaret", ctypes.c_long * 4)]


def focused_hwnd():
    """前台窗口里真正拥有键盘焦点的那个控件（记事本的编辑区、浏览器的输入框等）。"""
    hwnd = fg_window()
    if not hwnd:
        return None
    try:
        tid = user32.GetWindowThreadProcessId(hwnd, None)
        gti = _GUITHREADINFO()
        gti.cbSize = ctypes.sizeof(_GUITHREADINFO)
        if user32.GetGUIThreadInfo(tid, ctypes.byref(gti)) and gti.hwndFocus:
            return int(gti.hwndFocus)
    except Exception:
        pass
    return hwnd


def _post_msg(hwnd, msg, timeout=400):
    """给控件发消息（不经过键盘注入）。目标卡死时不会把我们也卡住。"""
    if not hwnd:
        return False
    try:
        res = ctypes.c_size_t(0)
        r = user32.SendMessageTimeoutW(ctypes.c_void_p(hwnd), msg, 0, 0,
                                       SMTO_ABORTIFHUNG | SMTO_BLOCK, timeout,
                                       ctypes.byref(res))
        return bool(r)
    except Exception:
        return False


def grab_selection(use_cut=False, via_message=False):
    """取当前选中内容。返回 (文本或None, 原剪贴板备份)。

    三种取法，按可靠性递进：
      1. 注入 Ctrl+C（通用，但会被安全软件/权限拦）
      2. 给焦点控件发 WM_COPY（标准控件如记事本/Word 必成功，不用注入）
      3. 注入 Ctrl+X（有些终端只认剪切）
    """
    backup = clip_get()
    clip_set("")
    time.sleep(0.03)
    if via_message:
        _post_msg(focused_hwnd(), WM_COPY)
    else:
        _send_combo("ctrl", "x" if use_cut else "c")
    text = ""
    for _ in range(18):
        time.sleep(0.04)
        cur = clip_get()
        if cur.strip():
            text = cur
            break
    if not text.strip():
        clip_set(backup)
        return None, backup
    return text, backup


def focus_text(limit=6000):
    """读焦点控件里的文字（标准 Win32 控件有效，浏览器/终端一般读不到）。"""
    hwnd = focused_hwnd()
    if not hwnd:
        return None
    try:
        n = user32.SendMessageW(ctypes.c_void_p(hwnd), 0x000E, 0, 0)   # WM_GETTEXTLENGTH
        if not n:
            return None
        n = min(int(n), limit)
        buf = ctypes.create_unicode_buffer(n + 8)
        got = user32.SendMessageW(ctypes.c_void_p(hwnd), 0x000D, n + 1, buf)  # WM_GETTEXT
        if not got:
            return None
        return buf.value
    except Exception:
        return None


def insert_text(text, backup_clip=None):
    """把英文写回当前焦点位置。返回 (成功?, 英文是否留在剪贴板)。

    关键安全点：写入剪贴板后必须校验内容，校验不过绝不发粘贴，
    否则剪贴板是空的就会把用户选中的原文直接删掉。
    """
    if not text:
        return False, False
    method = CFG.get("insert_method", "auto")
    if method == "type":
        try:
            keyboard.write(to_ascii(text), delay=0)
            return True, False
        except Exception:
            method = "paste"
    if method in ("paste", "message", "auto"):
        prev = clip_get()
        if not clip_set(text):
            return False, False
        time.sleep(0.06)
        if (clip_get() or "").strip()[:30] != text.strip()[:30]:
            # 剪贴板没写进去（被别的程序占用），此时粘贴会误删原文，直接放弃
            return False, False
        if method in ("paste", "auto"):
            _send_combo("ctrl", "v")
        if method == "message":
            _post_msg(focused_hwnd(), WM_PASTE)
        time.sleep(float(CFG.get("paste_delay", 0.35)))

        if method == "auto":
            # 注入被拦时（安全软件、管理员程序）上面那下没落地。
            # 只在「控件里还是中文、且不是我们要的英文」时才补发 WM_PASTE，避免重复粘贴。
            cur = focus_text()
            if cur and CJK_RE.search(cur) and text.strip()[:18] not in cur:
                log("Ctrl+V 没落地，补发 WM_PASTE")
                _post_msg(focused_hwnd(), WM_PASTE)
                time.sleep(0.2)

        if CFG.get("restore_clipboard", True):
            clip_set(backup_clip if backup_clip is not None else prev)
        return True, True
    return False, False


def to_ascii(s):
    table = {
        "\u2014": "-", "\u2013": "-", "\u2018": "'", "\u2019": "'",
        "\u201c": '"', "\u201d": '"', "\u2026": "...", "\u00a0": " ", "\u00b7": "-",
    }
    for a, b in table.items():
        s = s.replace(a, b)
    return "".join(ch if ord(ch) < 128 else "?" for ch in s)


# --------------------------------------------------------------------------
# 翻译引擎（多源自动降级）
# --------------------------------------------------------------------------
def _http(url, data=None, headers=None, timeout=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    with urllib.request.urlopen(req, timeout=timeout or CFG.get("http_timeout", 10)) as r:
        return r.read().decode("utf-8", "ignore")


def _split_chunks(text, limit):
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) <= limit:
        return [text]
    parts = re.split(r"(?<=[。！？；!?;\n])", text)
    chunks, buf = [], ""
    for p in parts:
        while len(p) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(p[:limit])
            p = p[limit:]
        if len(buf) + len(p) <= limit:
            buf += p
        else:
            if buf:
                chunks.append(buf)
            buf = p
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


def tr_mymemory(text, direction="zh2en"):
    src, dst = DIRECTIONS.get(direction, DIRECTIONS["zh2en"])
    out = []
    for chunk in _split_chunks(text, 450):
        url = "https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(
            {"q": chunk, "langpair": f"{src}|{dst}"}
        )
        js = json.loads(_http(url))
        if int(js.get("responseStatus", 0)) != 200:
            raise RuntimeError(js.get("responseDetails") or f"status={js.get('responseStatus')}")
        t = (js.get("responseData") or {}).get("translatedText") or ""
        t = html_mod.unescape(t).strip()
        if not t:
            raise RuntimeError("空结果")
        out.append(t)
    return " ".join(out)


def tr_google(text, direction="zh2en"):
    src, dst = DIRECTIONS.get(direction, DIRECTIONS["zh2en"])
    out = []
    for chunk in _split_chunks(text, 1800):
        url = "https://translate.googleapis.com/translate_a/single?" + urllib.parse.urlencode(
            {"client": "gtx", "sl": src, "tl": dst, "dt": "t", "q": chunk}
        )
        js = json.loads(_http(url))
        segs = [s[0] for s in js[0] if s and s[0]]
        t = "".join(segs).strip()
        if not t:
            raise RuntimeError("空结果")
        out.append(t)
    return "".join(out)


def tr_youdao(text, direction="zh2en"):
    if direction == "en2zh":
        pair = {"from": "EN", "to": "zh-CHS"}
    else:
        pair = {"from": "zh-CHS", "to": "en"}
    data = urllib.parse.urlencode(
        {"i": text, "doctype": "json", "version": "2.1", "keyfrom": "fanyi.web", **pair}
    ).encode()
    raw = _http("https://aidemo.youdao.com/trans", data,
                {"Content-Type": "application/x-www-form-urlencoded"})
    js = json.loads(raw)
    t = (js.get("translate_result") or "").strip()
    if not t:
        raise RuntimeError("空结果")
    return t


PROVIDERS = {"mymemory": tr_mymemory, "google": tr_google, "youdao": tr_youdao}

_cache = {}
_cache_lock = threading.Lock()


def detect_direction(text):
    """有中文 → 中译英；没有中文 → 英译中。"""
    return "zh2en" if CJK_RE.search(text or "") else "en2zh"


def translate(text, direction=None):
    """返回 (译文, 引擎名, 耗时秒, 错误信息)。

    direction: "zh2en" / "en2zh"，不传就按内容自动判断。
    """
    text = (text or "").strip()
    if not text:
        return "", "", 0.0, "内容为空"
    direction = direction or detect_direction(text)
    key = (direction, text)
    with _cache_lock:
        if key in _cache:
            return _cache[key][0], _cache[key][1] + "(缓存)", 0.0, None

    errors = []
    for name in CFG.get("providers", ["mymemory"]):
        fn = PROVIDERS.get(name)
        if not fn:
            continue
        t0 = time.time()
        try:
            result = fn(text, direction)
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__} {e}")
            continue
        cost = time.time() - t0
        with _cache_lock:
            if len(_cache) > 800:
                _cache.clear()
            _cache[key] = (result, name)
        return result, name, cost, None
    return "", "", 0.0, "所有翻译源都失败了 -> " + " | ".join(errors)


# --------------------------------------------------------------------------
# UI（Tk 主线程 + 队列）
# --------------------------------------------------------------------------
ui_queue = queue.Queue()
ui_ready = threading.Event()
_ui_root = None
_PANEL = []

FONT = ("Microsoft YaHei UI", 11)
FONT_SMALL = ("Microsoft YaHei UI", 9)


def post(fn):
    if not HAS_TK or _ui_root is None:
        try:
            fn()
        except Exception:
            pass
        return
    ui_queue.put(fn)


def poll_ui():
    if _ui_root is None:
        return
    try:
        while True:
            fn = ui_queue.get_nowait()
            try:
                fn()
            except Exception as e:
                print(f"[UI错误] {e}")
    except queue.Empty:
        pass
    except Exception:
        pass
    try:
        _ui_root.after(40, poll_ui)
    except Exception:
        pass


def mouse_pos():
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def toast(text, ms=2600):
    """右下角轻提示，用于「剪贴板已翻译」这类无 UI 反馈。"""
    if not HAS_TK or _ui_root is None:
        print("[提示]", text.replace("\n", " "))
        return

    win = tk.Toplevel(_ui_root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    frame = tk.Frame(win, bg="#20242e", padx=14, pady=10)
    frame.pack()
    tk.Label(frame, text=text, bg="#20242e", fg="#f2f4f8", font=FONT_SMALL,
             justify="left", wraplength=420).pack()
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"+{sw - w - 26}+{sh - h - 70}")
    win.after(ms, win.destroy)


_POPUPS = []


class ResultPopup:
    """只读译文弹窗：显示原文 + 译文，可一键复制，不碰原窗口的内容。"""

    def __init__(self, source="", translated="", engine="", cost=0.0):
        if not HAS_TK:
            print("[译文]", translated)
            return
        self.translated = translated
        self._tk = tk

        win = tk.Toplevel(_ui_root)
        self.win = win
        win.title("英 → 中")
        win.configure(bg="#ffffff")
        win.attributes("-topmost", True)
        win.resizable(False, False)

        pad = tk.Frame(win, bg="#ffffff", padx=14, pady=12)
        pad.pack(fill="both", expand=True)

        src_show = source if len(source) <= 220 else source[:220] + "…"
        tk.Label(pad, text=src_show, justify="left", anchor="w",
                 bg="#ffffff", fg="#8b95a5", font=FONT_SMALL,
                 wraplength=470).pack(fill="x")

        tk.Frame(pad, bg="#eef1f5", height=1).pack(fill="x", pady=(9, 0))

        # 按内容长度估算行数，长句自动加高（最多 12 行，超出滚动）
        est_lines = max(2, min(12, (len(translated) // 34) + 1 + translated.count("\n")))
        self.txt = tk.Text(pad, height=est_lines, width=46, font=("Microsoft YaHei UI", 12),
                           wrap="word", relief="flat", bd=0, bg="#ffffff", fg="#101828",
                           padx=0, pady=8, highlightthickness=0, state="disabled",
                           cursor="arrow")
        self.txt.pack(fill="x")
        self.txt.configure(state="normal")
        self.txt.insert("1.0", translated)
        self.txt.configure(state="disabled")

        bar = tk.Frame(pad, bg="#ffffff")
        bar.pack(fill="x", pady=(6, 0))
        info = engine + (f" · {int(cost * 1000)}ms" if cost else "")
        self.status = tk.Label(bar, text=info, bg="#ffffff", fg="#9aa3b2",
                               font=FONT_SMALL, anchor="w")
        self.status.pack(side="left")

        mk = lambda t, c, fg, cmd: tk.Button(bar, text=t, command=cmd, font=FONT_SMALL,
                                            bg=c, fg=fg, relief="flat", bd=0,
                                            padx=12, pady=5, cursor="hand2",
                                            activebackground=c, activeforeground=fg)
        mk("关闭", "#eef1f5", "#4b5563", self.close).pack(side="right", padx=(6, 0))
        mk("复制译文", "#2f6bff", "#ffffff", self.copy).pack(side="right")

        win.bind("<Escape>", lambda e: self.close())
        win.protocol("WM_DELETE_WINDOW", self.close)

        win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        mx, my = mouse_pos()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        x = min(max(mx - w // 2, 16), sw - w - 16)
        y = min(max(my + 22, 16), sh - h - 60)
        win.geometry(f"+{x}+{y}")
        win.lift()
        try:
            win.focus_force()
        except Exception:
            pass
        _POPUPS.append(self)

    def copy(self):
        ok = clip_set(self.translated)
        try:
            self.status.configure(text="已复制到剪贴板" if ok else "复制失败：剪贴板被占用",
                                  fg="#16a34a" if ok else "#dc2626")
        except Exception:
            pass

    def close(self):
        try:
            self.win.destroy()
        except Exception:
            pass
        if self in _POPUPS:
            _POPUPS.remove(self)


class Popup:
    """兜底输入框：敲中文，回车出英文，Ctrl+Enter 直接贴回原来的窗口。"""

    def __init__(self, target_hwnd=None, prefill="", pinned=False):
        if not HAS_TK:
            print("[提示] 当前 Python 没有 tkinter，弹不出输入框。请用「启动翻译.bat」启动（会自动选用系统 Python）。")
            return

        self.target = target_hwnd
        self.busy = False
        self.english = ""
        self.pinned = pinned
        self._last_src = ""
        self._timer = None
        self._tk = tk

        win = tk.Toplevel(_ui_root)
        self.win = win
        win.title("翻译台 · 中 → 英" if pinned else "中 → 英")
        win.configure(bg="#ffffff")
        win.attributes("-topmost", True)
        win.resizable(False, False)

        pad = tk.Frame(win, bg="#ffffff", padx=12, pady=10)
        pad.pack(fill="both", expand=True)

        hint = ("点进下面的框，用你平时的输入法打中文，停一下就出英文\n"
                "Ctrl+Enter 贴回上一个窗口 · 右上角 × 退出工具"
                if pinned else
                "输入中文，回车翻译 · Ctrl+Enter 直接贴回原窗口 · Esc 关闭")
        tk.Label(pad, text=hint, justify="left",
                 bg="#ffffff", fg="#7a8291", font=FONT_SMALL, anchor="w").pack(fill="x", pady=(0, 6))

        self.src = tk.Text(pad, height=3, width=52, font=FONT, wrap="word",
                           relief="flat", bd=0, bg="#f6f7f9", fg="#1f2430",
                           insertbackground="#1f2430", padx=10, pady=8,
                           highlightthickness=1, highlightbackground="#dde2ea",
                           highlightcolor="#3b82f6")
        self.src.pack(fill="x")

        self.dst = tk.Text(pad, height=4, width=52, font=FONT, wrap="word",
                           relief="flat", bd=0, bg="#ffffff", fg="#0f172a",
                           padx=10, pady=8, highlightthickness=1,
                           highlightbackground="#e6eaf0", state="disabled")
        self.dst.pack(fill="x", pady=(8, 0))

        bar = tk.Frame(pad, bg="#ffffff")
        bar.pack(fill="x", pady=(8, 0))
        self.status = tk.Label(bar, text="就绪", bg="#ffffff", fg="#9aa3b2", font=FONT_SMALL, anchor="w")
        self.status.pack(side="left")

        mk = lambda t, c, fg, cmd: tk.Button(bar, text=t, command=cmd, font=FONT_SMALL,
                                            bg=c, fg=fg, relief="flat", bd=0,
                                            padx=12, pady=5, cursor="hand2",
                                            activebackground=c, activeforeground=fg)
        mk("关闭", "#eef1f5", "#4b5563", self.close).pack(side="right", padx=(6, 0))
        mk("复制", "#eef1f5", "#4b5563", self.copy).pack(side="right", padx=(6, 0))
        self.btn_paste = mk("贴回原窗口", "#eef1f5", "#4b5563", self.paste_back)
        self.btn_paste.pack(side="right", padx=(6, 0))
        self.btn_tr = mk("翻译", "#2f6bff", "#ffffff", lambda: self.do_translate(paste=False))
        self.btn_tr.pack(side="right")

        self.src.bind("<Return>", self._on_enter)
        self.src.bind("<Shift-Return>", lambda e: None)
        self.src.bind("<Control-Return>", self._on_ctrl_enter)
        if pinned:
            # 常驻面板：Esc 只清空不关闭；停手 0.7 秒自动翻译
            self.src.bind("<KeyRelease>", self._on_key_release)
            win.bind("<Escape>", lambda e: self.clear())
            win.protocol("WM_DELETE_WINDOW", quit_app)
        else:
            self.src.bind("<Escape>", lambda e: self.close())
            win.bind("<Escape>", lambda e: self.close())
            win.protocol("WM_DELETE_WINDOW", self.close)

        if prefill:
            self.src.insert("1.0", prefill)

        win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        mx, my = mouse_pos()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        if pinned:
            # 常驻面板固定停在右下角
            x = max(sw - w - 70, 20)
            y = max(sh - h - 130, 20)
        else:
            x = min(max(mx - w // 2, 20), sw - w - 20)
            y = min(max(my + 24, 20), sh - h - 60)
        win.geometry(f"+{x}+{y}")
        win.lift()
        if not pinned:
            win.focus_force()
        self.src.focus_set()

    # ---------------- 交互 ----------------
    def clear(self):
        try:
            self.src.delete("1.0", "end")
        except Exception:
            pass
        self.set_dst("")
        self.english = ""
        self._last_src = ""
        self.set_status("已清空")

    def _on_key_release(self, event):
        if event.keysym in ("Return", "Escape", "Tab", "Up", "Down", "Left", "Right",
                            "Home", "End", "Prior", "Next", "Shift_L", "Shift_R",
                            "Control_L", "Control_R", "Alt_L", "Alt_R"):
            return
        self._schedule_translate()

    def _schedule_translate(self):
        if self._timer:
            try:
                self.win.after_cancel(self._timer)
            except Exception:
                pass
            self._timer = None
        try:
            self._timer = self.win.after(700, self._auto_translate)
        except Exception:
            self._timer = None

    def _auto_translate(self):
        self._timer = None
        text = self.get_src()
        if not text or text == self._last_src:
            return
        self.do_translate(paste=False)

    def _on_enter(self, event):
        self.do_translate(paste=False)
        return "break"

    def _on_ctrl_enter(self, event):
        self.do_translate(paste=True)
        return "break"

    def get_src(self):
        return self.src.get("1.0", "end").strip()

    def set_status(self, s, color="#9aa3b2"):
        self.status.configure(text=s, fg=color)

    def set_dst(self, s):
        self.dst.configure(state="normal")
        self.dst.delete("1.0", "end")
        self.dst.insert("1.0", s)
        self.dst.configure(state="disabled")

    def do_translate(self, paste=False):
        if self.busy:
            return
        text = self.get_src()
        if not text:
            self.set_status("先输入点中文", "#d97706")
            return
        if not CJK_RE.search(text):
            self.set_status("这段里没有中文，已跳过", "#d97706")
            return
        self.busy = True
        self.set_status("翻译中…")

        def work():
            result, engine, cost, err = translate(text)

            def apply():
                self.busy = False
                if err:
                    self.set_status("失败：" + err[:120], "#dc2626")
                    return
                self.english = result
                self._last_src = text
                self.set_dst(result)
                self.set_status(f"{engine} · {int(cost * 1000)}ms")
                if paste:
                    self.paste_back()
                elif self.pinned:
                    # 翻译期间又改了字，补一次
                    if self.get_src() != text:
                        self._schedule_translate()
                else:
                    try:
                        self.src.focus_set()
                    except Exception:
                        pass

            post(apply)

        threading.Thread(target=work, daemon=True).start()

    def copy(self):
        txt = self.english or self.dst.get("1.0", "end").strip()
        if not txt:
            self.set_status("还没有译文", "#d97706")
            return
        clip_set(txt)
        self.set_status("已复制到剪贴板")

    def paste_back(self):
        txt = self.english or self.dst.get("1.0", "end").strip()
        if not txt:
            self.do_translate(paste=True)
            return
        target = self.target
        self.close()
        time.sleep(0.02)
        if target:
            activate_window(target)
        if not clip_set(txt):
            return
        time.sleep(0.08)
        if (clip_get() or "").strip()[:30] != txt.strip()[:30]:
            return
        _send_combo("ctrl", "v")
        log("弹窗译文已贴回原窗口")

    def close(self):
        try:
            self.win.destroy()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 业务流程
# --------------------------------------------------------------------------
_lock = threading.Lock()


def flow_translate():
    if not _lock.acquire(blocking=False):
        return
    try:
        log(f"触发 {CFG['hotkey_translate']}")
        hwnd = fg_window()
        wait_modifiers_release()
        time.sleep(0.05)

        text, backup = grab_selection()
        how = "Ctrl+C"
        cut_used = False
        if not text:                                   # 有些程序会吞掉 Ctrl+C
            time.sleep(0.12)
            text, backup = grab_selection()
        if not text:                                   # 绕过注入，直接问控件要
            log("注入复制失败，改用 WM_COPY")
            time.sleep(0.08)
            text, backup = grab_selection(via_message=True)
            how = "WM_COPY"
        if not text:                                   # 再不行就退回 Ctrl+X
            log("WM_COPY 也没拿到，改用 Ctrl+X")
            time.sleep(0.12)
            text, backup = grab_selection(use_cut=True)
            cut_used = bool(text)
            how = "Ctrl+X"
        if not text:
            log("没取到选中内容 → 弹输入框（先确认你确实选中了文字）")
            post(lambda: Popup(target_hwnd=hwnd))
            return

        log(f"取到 {len(text)} 字（{how}）：{text[:36]!r}")
        if not CJK_RE.search(text):
            if cut_used:                               # 剪掉的东西得还回去
                insert_text(text, backup)
            log("不含中文 → 跳过（英文可用反向热键看中文）")
            post(lambda: toast(f"这段不含中文。要看中文译文，按 "
                               f"{CFG['hotkey_translate_reverse']}", 3000))
            return

        result, engine, cost, err = translate(text)
        if err:
            log(f"翻译失败：{err[:100]}")
            if cut_used:
                insert_text(text, backup)              # 剪切模式失败了，原文贴回去
            post(lambda: toast("翻译失败：" + err[:120], 4200))
            return

        log(f"译文({engine} {int(cost * 1000)}ms)：{result[:60]!r}")
        ok, in_clipboard = insert_text(result, backup)
        snippet = (text[:26] + "…") if len(text) > 26 else text
        if ok:
            log("已替换 ✓")
            post(lambda: toast(f"{snippet}  →  {engine} {int(cost * 1000)}ms", 1400))
        elif in_clipboard:
            log("粘贴动作发出去了，但不确定成功")
            post(lambda: toast(f"英文已复制，但没能自动粘贴。\n{snippet} → {result[:100]}", 4000))
        else:
            log("回写失败，原文未改动")
            post(lambda: toast(f"回写失败（剪贴板被占用），原文没动。\n译文：{result[:120]}", 4500))
    finally:
        _lock.release()


def flow_translate_reverse():
    """选中英文 → 按 Alt+; → 弹出中文译文（只读，不改原文）。"""
    if not _lock.acquire(blocking=False):
        return
    try:
        log(f"触发 {CFG['hotkey_translate_reverse']}（英 → 中）")
        hwnd = fg_window()
        wait_modifiers_release()
        time.sleep(0.05)

        text, backup = grab_selection()
        if not text:
            time.sleep(0.12)
            text, backup = grab_selection()
        if not text:
            log("Ctrl+C 没拿到，改用 WM_COPY")
            time.sleep(0.08)
            text, backup = grab_selection(via_message=True)
        # 注意：这里绝不使用 Ctrl+X —— 只是看一眼意思，不该把用户原文剪走
        if not text:
            log("没取到选中内容")
            post(lambda: toast("没取到选中的英文。先框选一段文字再按 "
                               + CFG["hotkey_translate_reverse"], 3200))
            return

        log(f"取到 {len(text)} 字：{text[:40]!r}")
        if CJK_RE.search(text):
            log("选中内容含中文，不是英译中")
            post(lambda: toast(f"这段是中文，用 {CFG['hotkey_translate']} 转英文", 2600))
            return

        result, engine, cost, err = translate(text, "en2zh")
        if err:
            log(f"翻译失败：{err[:100]}")
            post(lambda: toast("翻译失败：" + err[:120], 4200))
            return

        log(f"译文({engine} {int(cost * 1000)}ms)：{result[:60]!r}")
        post(lambda: ResultPopup(source=text, translated=result,
                                 engine=engine, cost=cost))
    finally:
        _lock.release()


def flow_clipboard():
    if not _lock.acquire(blocking=False):
        return
    try:
        wait_modifiers_release()
        text = clip_get()
        if not text.strip():
            post(lambda: toast("剪贴板是空的"))
            return
        if not CJK_RE.search(text):
            post(lambda: toast("剪贴板内容不含中文"))
            return
        result, engine, cost, err = translate(text)
        if err:
            post(lambda: toast("翻译失败：" + err[:120], 4200))
            return
        clip_set(result)
        post(lambda: toast(f"已转为英文并放回剪贴板（{engine}）\n{result[:180]}", 4000))
    finally:
        _lock.release()


def quit_app():
    post(lambda: toast("zh2en 已退出", 900))
    time.sleep(0.4)
    os._exit(0)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def single_instance():
    try:
        h = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\zh2en_single_instance")
        return ctypes.windll.kernel32.GetLastError() != 183, h
    except Exception:
        return True, None


def run_ui():
    global _ui_root
    if not HAS_TK:
        print("[警告] 当前 Python 缺少 tkinter：只能使用「选中即译」功能，弹窗不可用。")
        print("       建议用系统 Python 启动（启动翻译.bat 会自动优先用 py 启动器）。")
        ui_ready.set()
        return

    _ui_root = tk.Tk()
    _ui_root.withdraw()
    _ui_root.after(40, poll_ui)
    ui_ready.set()
    _ui_root.mainloop()


def run_panel():
    """翻译台：常驻小窗口，完全不依赖全局快捷键。"""
    if not HAS_TK:
        print("[错误] 当前 Python 没有 tkinter，翻译台打不开。")
        print("       请双击「翻译台.bat」启动（会自动优先用系统 Python 3.12）。")
        input("按回车退出…")
        return

    ok, _m = single_instance()
    if not ok:
        print("工具已经在运行了（后台还有一个实例）。")
        print("先关掉它，或按 Ctrl+Alt+Q 退出后再启动翻译台。")
        time.sleep(3)
        return

    print("=" * 56)
    print("  zh2en · 翻译台（不用快捷键，纯窗口）")
    print("=" * 56)
    print("  屏幕右下角那个小窗口就是它。")
    print("  1) 点一下里面的输入框")
    print("  2) 用你平时的输入法打中文，停一下就自动出英文")
    print("  3) 要贴回刚才的窗口：按 Ctrl+Enter")
    print("  右上角 × 退出工具 · Esc 只清空输入")
    print("-" * 56)
    print(f"  引擎: {' → '.join(CFG.get('providers', []))}")
    print("  这个黑窗口可以最小化，但别关（关掉工具就退了）。")
    print("=" * 56, flush=True)

    threading.Thread(target=run_ui, daemon=True).start()
    ui_ready.wait(5)

    def make():
        try:
            _PANEL.append(Popup(target_hwnd=None, pinned=True))
        except Exception as ex:
            import traceback
            print("[错误] 翻译台窗口创建失败：")
            traceback.print_exc()

    post(make)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass


def check():
    """自检：把每个环节单独验一遍，出问题能直接看出卡在哪。"""
    print("=" * 58)
    print("  zh2en 自检（环境 / 依赖 / 剪贴板 / 翻译 / 热键）")
    print("=" * 58)
    ok_all = True
    hints = []

    print(f"[1] Python  {sys.version.split()[0]}")
    print(f"      {sys.executable}")

    print("[2] 依赖 keyboard / pyperclip")
    print("      OK")

    print("[3] tkinter（弹窗模式需要）")
    if HAS_TK:
        print(f"      OK  Tk {tk.TkVersion}")
    else:
        print("      缺失 → 只能「选中即替换」，弹窗不可用")
        hints.append("换系统 Python 启动：用 启动翻译.bat（它优先用 py 启动器）")
        ok_all = False

    print("[4] 剪贴板读写")
    old = clip_get()
    probe = "zh2en-check-%d" % int(time.time())
    if clip_set(probe) and clip_get() == probe:
        print("      OK")
    else:
        print("      写读不一致，可能被安全软件拦住了")
        hints.append("把脚本目录加入杀软白名单，或尝试以管理员身份运行")
        ok_all = False
    clip_set(old)

    print("[5] 翻译接口")
    t0 = time.time()
    res, engine, cost, err = translate("今天的构建已经修好了", "zh2en")
    if err:
        print(f"      失败：{err[:150]}")
        hints.append("网络被拦：检查代理/防火墙，或改 config.json 里的 providers")
        ok_all = False
    else:
        print(f"      {engine}  {int(cost * 1000)}ms")
        print(f"      今天的构建已经修好了 → {res[:80]}")

    res2, engine2, cost2, err2 = translate("Ship it tomorrow morning.", "en2zh")
    if err2:
        print(f"      英译中失败：{err2[:150]}")
        ok_all = False
    else:
        print(f"      {engine2}  {int(cost2 * 1000)}ms")
        print(f"      Ship it tomorrow morning. → {res2[:80]}")

    print("[6] 快捷键能否被系统注册（RegisterHotKey）")
    bad = []
    for combo in (CFG["hotkey_translate"], CFG["hotkey_translate_reverse"],
                  CFG["hotkey_clipboard"], CFG["hotkey_quit"]):
        ok, code = hotkey_test(combo)
        mods, vk = combo_to_hotkey(combo)
        detail = f"mods=0x{mods:02X} vk=0x{vk:02X}" if vk else "键名解析失败"
        if ok:
            print(f"      OK    {combo:<12} {detail}")
        else:
            why = {1409: "已被别的程序占用", -1: "键名解析失败"}.get(code, f"错误码 {code}")
            print(f"      失败  {combo:<12} {why}  {detail}")
            bad.append(combo)
    if bad:
        print("      → 改 config.json 换键位；上面这几项注册不上，按了不会触发。")
        print("      → 如果工具已经在运行，这里会因为「已被自己占用」而报 1409，属正常。")
        hints.append("快捷键注册失败：换 config.json 里的键位，或关掉占用它的软件")
        ok_all = False

    print("-" * 58)
    if ok_all:
        print("结论：环境正常。两个用法任选：")
        print("      ① 双击 翻译台.bat —— 右下角小窗口，打字就出英文，不用快捷键（最稳）")
        print(f"      ② 双击 启动翻译.bat —— 选中中文按 {CFG['hotkey_translate']}，"
              f"选中英文按 {CFG['hotkey_translate_reverse']}")
        print("      注意：目标程序若是管理员权限运行，本工具也要用管理员身份启动。")
    else:
        print("结论：有问题，按下面提示处理：")
        for i, h in enumerate(dict.fromkeys(hints), 1):
            print(f"      {i}. {h}")
    print("=" * 58)
    return ok_all


def main():
    global _ui_root

    if len(sys.argv) > 1 and sys.argv[1] in ("--check", "-c"):
        check()
        return

    if len(sys.argv) > 1 and sys.argv[1] in ("--keys", "-k"):
        key_probe()
        return

    if len(sys.argv) > 1 and sys.argv[1] in ("--panel", "-p"):
        run_panel()
        return

    if len(sys.argv) > 2 and sys.argv[1] == "--tr":
        result, engine, cost, err = translate(" ".join(sys.argv[2:]), "zh2en")
        if err:
            print("失败:", err)
            sys.exit(1)
        print(f"[{engine} {int(cost * 1000)}ms] {result}")
        return

    if len(sys.argv) > 2 and sys.argv[1] in ("--zh", "--tr2"):
        result, engine, cost, err = translate(" ".join(sys.argv[2:]), "en2zh")
        if err:
            print("失败:", err)
            sys.exit(1)
        print(f"[{engine} {int(cost * 1000)}ms] {result}")
        return

    ok, _mutex = single_instance()
    if not ok:
        print("zh2en 已经在运行了（后台还有一个实例）。")
        print("如果没反应，先在任务管理器里结束 python.exe / pythonw.exe 再启动。")
        time.sleep(3)
        return

    print("=" * 52)
    print("  zh2en · 中文输入即时转英文")
    print("=" * 52)
    print(f"  {CFG['hotkey_translate']:<14} 选中中文 → 英文替换 / 无选中 → 弹出输入框")
    print(f"  {CFG['hotkey_translate_reverse']:<14} 选中英文 → 弹出中文译文（不改原文）")
    print(f"  {CFG['hotkey_clipboard']:<14} 剪贴板中文 → 英文")
    print(f"  {CFG['hotkey_quit']:<14} 退出")
    print("-" * 52)
    print(f"  引擎: {' → '.join(CFG.get('providers', []))}")
    print("  目标程序若是管理员权限运行，本工具也需管理员权限才能收发按键。")
    print("  本窗口关掉 = 工具退出。")
    print("=" * 52)
    print("  >> 已就绪。切到任意程序，选中一段中文，按 "
          + CFG["hotkey_translate"].upper().replace("+", "+") + " 试试。")
    print("  >> 按了没反应：关掉本窗口，双击「自检.bat」看卡在哪。")
    print("=" * 52)

    threading.Thread(target=run_ui, daemon=True).start()
    ui_ready.wait(5)

    bindings = [
        (CFG["hotkey_translate"], flow_translate),
        (CFG["hotkey_translate_reverse"], flow_translate_reverse),
        (CFG["hotkey_clipboard"], flow_clipboard),
        (CFG["hotkey_quit"], quit_app),
    ]
    listener = HotkeyListener(bindings).start()

    if listener.registered:
        print("  已注册热键（系统级 RegisterHotKey）:")
        for _i, combo, _f in listener.registered:
            print(f"      {combo}")
    for combo, code in listener.failed:
        why = {1409: "已被别的程序占用", -1: "这个键名解析不了"}.get(code, f"错误码 {code}")
        print(f"  [失败] {combo} 注册不上：{why}")

    if not any(c == CFG["hotkey_translate"] for _i, c, _f in listener.registered):
        print("=" * 52)
        print(f"  [注意] 主快捷键 {CFG['hotkey_translate']} 没注册上，「框选替换」用不了。")
        print("         改 config.json 换一个组合（例如 ctrl+alt+t），")
        print("         或先关掉占用这个键的软件再启动。")
        print("=" * 52)

    if not listener.registered:
        print("  全部注册失败，改用键盘钩子兜底（能不能收到事件看运气）…")
        matcher = HotkeyMatcher(bindings)
        try:
            keyboard.hook(matcher.on_event)
        except Exception as e:
            print(f"[错误] 连键盘钩子也装不上：{type(e).__name__} {e}")
            print("       双击「翻译台.bat」用窗口模式，或右键以管理员身份运行。")
            input("按回车退出…")
            return
        try:
            keyboard.wait()
        except KeyboardInterrupt:
            pass
        return

    print("=" * 52)
    print(f"  >> 已就绪。选中中文按 {CFG['hotkey_translate'].upper()} 转英文，")
    print(f"             选中英文按 {CFG['hotkey_translate_reverse'].upper()} 看中文。")
    print("=" * 52)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
