"""QuickShot: скриншоты по хоткею с нормальным редактором (рисование, стрелки, текст, размытие)."""
import base64
import ctypes
import ctypes.wintypes as wt
import faulthandler
import io
import json
import logging
import os
import sys
import threading
import time
import winreg
from datetime import datetime
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageGrab

APP_NAME = "QuickShot"
FROZEN = getattr(sys, "frozen", False)
RES_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
DATA_DIR = Path(os.environ["APPDATA"]) / "QuickShot"
CONFIG_PATH = DATA_DIR / "config.json"
# страница и снимок раздаются самим WebView2 с виртуального хоста (нативно, без Python http-сервера)
VHOST = "quickshot.local"
SHOT_PATH = RES_DIR / "ui" / "shot.raw"

DEFAULT_CONFIG = {
    "hotkey": "printscreen",
    "save_dir": "%USERPROFILE%\\Pictures\\Screenshots",
    "autostart": False,
}
HOTKEY_PRESETS = [("printscreen", "PrtSc"), ("ctrl+printscreen", "Ctrl + PrtSc"),
                  ("ctrl+shift+s", "Ctrl + Shift + S"), ("ctrl+shift+a", "Ctrl + Shift + A"),
                  ("alt+shift+s", "Alt + Shift + S")]

log = logging.getLogger("quickshot")
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
        except Exception as e:
            log.error("config.json битый, беру дефолт: %s", e)
    return cfg


def save_config(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def save_dir(cfg):
    p = Path(os.path.expandvars(cfg["save_dir"]))
    p.mkdir(parents=True, exist_ok=True)
    return p


def set_autostart(on):
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                         winreg.KEY_SET_VALUE)
    with key:
        if on:
            if FROZEN:
                cmd = f'"{sys.executable}"'
            else:
                cmd = f'"{Path(sys.executable).with_name("pythonw.exe")}" "{Path(__file__).resolve()}"'
            winreg.SetValueEx(key, "QuickShot", 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, "QuickShot")
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- hotkey

MODS = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
NAMED_KEYS = {"printscreen": 0x2C, "pause": 0x13, "insert": 0x2D, "home": 0x24, "end": 0x23,
              "pageup": 0x21, "pagedown": 0x22, "scrolllock": 0x91}


def parse_hotkey(s):
    mods, vk = MOD_NOREPEAT, None
    for part in s.lower().replace(" ", "").split("+"):
        if part in MODS:
            mods |= MODS[part]
        elif part in NAMED_KEYS:
            vk = NAMED_KEYS[part]
        elif part.startswith("f") and part[1:].isdigit():
            vk = 0x6F + int(part[1:])
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
    if vk is None:
        raise ValueError(f"не понял хоткей: {s}")
    return mods, vk


class Hotkey:
    def __init__(self, combo, callback):
        self.combo, self.callback = combo, callback
        self.tid = None
        self.error = None
        self.ok = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        self.ok.wait(2)

    def _run(self):
        self.tid = kernel32.GetCurrentThreadId()
        try:
            mods, vk = parse_hotkey(self.combo)
            if not user32.RegisterHotKey(None, 1, mods, vk):
                raise RuntimeError(f"хоткей {self.combo} уже занят другой программой")
        except Exception as e:
            self.error = str(e)
            self.ok.set()
            return
        self.ok.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                threading.Thread(target=self.callback, daemon=True).start()
        user32.UnregisterHotKey(None, 1)

    def stop(self):
        if self.tid:
            user32.PostThreadMessageW(self.tid, WM_QUIT, 0, 0)


# ---------------------------------------------------------------- screen

def virtual_screen():
    return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
            user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))


def monitors():
    """Прямоугольники мониторов относительно левого верхнего угла виртуального экрана."""
    vx, vy, _, _ = virtual_screen()
    out = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_int, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)

    def cb(h, hdc, r, lp):
        r = r.contents
        out.append({"x": r.left - vx, "y": r.top - vy, "w": r.right - r.left, "h": r.bottom - r.top})
        return 1

    user32.EnumDisplayMonitors(None, None, proc(cb), 0)
    return out


def cursor_pos():
    vx, vy, _, _ = virtual_screen()
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x - vx, p.y - vy


# ---------------------------------------------------------------- clipboard

kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p


def _clip_put(fmt, data):
    h = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    user32.SetClipboardData(fmt, h)


def copy_image(img, png_bytes=None):
    """В буфер кладём DIB (вставляется везде) и PNG (для программ, которые его понимают)."""
    bmp = io.BytesIO()
    img.convert("RGB").save(bmp, "BMP")
    if png_bytes is None:
        png = io.BytesIO()
        img.save(png, "PNG")
        png_bytes = png.getvalue()
    for _ in range(10):  # буфер может быть занят другой программой
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("буфер обмена занят")
    try:
        user32.EmptyClipboard()
        _clip_put(8, bmp.getvalue()[14:])  # CF_DIB = BMP без файлового заголовка
        _clip_put(user32.RegisterClipboardFormatW("PNG"), png_bytes)
    finally:
        user32.CloseClipboard()


# ---------------------------------------------------------------- api

def decode_png(data_url):
    raw = base64.b64decode(data_url.split(",", 1)[1])
    return Image.open(io.BytesIO(raw)), raw


class Api:
    def __init__(self, app):
        self._app = app

    def ready(self):
        self._app.show_overlay()

    def cancel(self):
        self._app.hide_overlay()

    def hide(self):
        """Спрятать окно, не сбрасывая страницу: она ещё экспортирует картинку."""
        self._app.visible = False
        self._app.window.hide()

    def copy(self, data_url):
        self._app.hide_overlay()
        try:
            copy_image(*decode_png(data_url))
            self._app.notify("Скриншот скопирован")
        except Exception as e:
            log.exception("copy")
            self._app.notify(f"Не удалось скопировать: {e}")

    def save(self, data_url, ask=False):
        a = self._app
        path = save_dir(a.cfg) / f"Скрин_{datetime.now():%Y-%m-%d_%H-%M-%S}.png"
        if ask:
            import webview
            # диалог над оверлеем: оверлей всё равно topmost, поэтому прячем его заранее
            a.hide_overlay()
            r = a.window.create_file_dialog(webview.SAVE_DIALOG, directory=str(path.parent), save_filename=path.name,
                                            file_types=("PNG (*.png)", "JPEG (*.jpg)"))
            if not r:
                return
            path = Path(r if isinstance(r, str) else r[0])
        else:
            a.hide_overlay()
        try:
            img, raw = decode_png(data_url)
            if path.suffix.lower() in (".jpg", ".jpeg"):
                img.convert("RGB").save(path, quality=95)
            else:
                path.write_bytes(raw)  # PNG уже готов — пишем как есть
            copy_image(img, raw)
            a.notify(f"Сохранено и скопировано: {path.name}")
            log.info("saved %s", path)
        except Exception as e:
            log.exception("save")
            a.notify(f"Не удалось сохранить: {e}")


# ---------------------------------------------------------------- app

def make_icon(size=64):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64
    d.rounded_rectangle((3 * s, 3 * s, 61 * s, 61 * s), radius=14 * s, fill=(30, 32, 40, 255))
    w = max(2, int(5 * s))
    # уголки рамки выделения
    for x0, y0, dx, dy in ((14, 14, 1, 1), (50, 14, -1, 1), (14, 50, 1, -1), (50, 50, -1, -1)):
        d.line(((x0 * s, y0 * s), ((x0 + 12 * dx) * s, y0 * s)), fill=(255, 255, 255, 255), width=w)
        d.line(((x0 * s, y0 * s), (x0 * s, (y0 + 12 * dy) * s)), fill=(255, 255, 255, 255), width=w)
    d.ellipse((25 * s, 25 * s, 39 * s, 39 * s), fill=(77, 140, 255, 255))
    return img


class App:
    def __init__(self):
        self.cfg = load_config()
        if not CONFIG_PATH.exists():
            save_config(self.cfg)
        self.window = None
        self.hotkey = None
        self.busy = threading.Lock()
        self.visible = False
        self.page_ready = False
        self.mapped = False
        self.t0 = 0
        self.icon = pystray.Icon("QuickShot", make_icon(), APP_NAME, self._menu())

    def _menu(self):
        def hk_item(combo, label):
            return pystray.MenuItem(label, lambda: self.set_hotkey(combo),
                                    checked=lambda _: self.cfg["hotkey"] == combo, radio=True)

        return pystray.Menu(
            pystray.MenuItem("Сделать скриншот", lambda: self.capture(), default=True),
            pystray.MenuItem("Папка со скринами", lambda: os.startfile(save_dir(self.cfg))),
            pystray.MenuItem("Хоткей", pystray.Menu(*(hk_item(c, l) for c, l in HOTKEY_PRESETS))),
            pystray.MenuItem("Запускать с Windows", lambda: self.toggle_autostart(),
                             checked=lambda _: self.cfg["autostart"]),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", lambda: self.quit()),
        )

    def notify(self, text):
        try:
            self.icon.notify(text, APP_NAME)
        except Exception:
            pass

    def register_hotkey(self):
        if self.hotkey:
            self.hotkey.stop()
        self.hotkey = Hotkey(self.cfg["hotkey"], self.capture)
        label = dict(HOTKEY_PRESETS).get(self.cfg["hotkey"], self.cfg["hotkey"])
        self.icon.title = f"{APP_NAME} — {label}"
        if self.hotkey.error:
            log.error(self.hotkey.error)
            self.notify(f"Ошибка: {self.hotkey.error}. Выбери другой в меню «Хоткей»")

    def set_hotkey(self, combo):
        self.cfg["hotkey"] = combo
        save_config(self.cfg)
        self.register_hotkey()
        self.notify(f"Хоткей: {dict(HOTKEY_PRESETS)[combo]}")

    def toggle_autostart(self):
        self.cfg["autostart"] = not self.cfg["autostart"]
        try:
            set_autostart(self.cfg["autostart"])
        except OSError as e:
            log.error("autostart: %s", e)
        save_config(self.cfg)

    # --- capture flow: снимок → картинка в страницу → страница зовёт ready() → показываем оверлей

    def capture(self):
        if self.visible or not self.page_ready or not self.busy.acquire(blocking=False):
            return
        try:
            self.t0 = time.perf_counter()
            img = ImageGrab.grab(all_screens=True)
            # сырые пиксели RGBA в файл рядом со страницей: без PNG и base64 страница забирает их fetch'ем
            # и сразу собирает ImageData — это в разы быстрее, чем гнать картинку строкой через evaluate_js
            SHOT_PATH.write_bytes(img.tobytes("raw", "RGBX"))
            payload = {"url": f"{SHOT_PATH.name}?{time.time_ns()}", "w": img.width, "h": img.height,
                       "monitors": monitors(), "cursor": cursor_pos()}
            self.window.evaluate_js(f"window.loadShot({json.dumps(payload)}); 0")
            # если страница так и не позвала ready() — не блокируем хоткей навсегда
            threading.Timer(4, lambda: self.busy.locked() and not self.visible and self.busy.release()).start()
        except Exception:
            log.exception("capture")
            self.busy.release()

    def hwnd(self):
        try:
            return int(self.window.native.Handle.ToInt64())
        except Exception:
            return 0

    def show_overlay(self):
        vx, vy, vw, vh = virtual_screen()
        self.visible = True
        try:
            # всё делаем в UI-потоке WinForms: позиция, показ, активация и фокус в WebView2 (иначе клавиши не доходят)
            from System import Action
            from System.Drawing import Rectangle
            form = self.window.native

            def _show():
                form.Bounds = Rectangle(vx, vy, vw, vh)
                form.TopMost = True
                form.Show()
                form.Activate()

            def _focus():
                form.Activate()
                form.browser.webview.Focus()

            form.Invoke(Action(_show))
            form.BeginInvoke(Action(_focus))
        except Exception:
            log.exception("show overlay")
        h = self.hwnd()
        if h:
            user32.SetForegroundWindow(h)
            r = wt.RECT()
            user32.GetWindowRect(h, ctypes.byref(r))
            log.info("overlay %dx%d shown in %.0f ms", r.right - r.left, r.bottom - r.top,
                     (time.perf_counter() - self.t0) * 1000)
        if self.busy.locked():
            self.busy.release()

    def hide_overlay(self):
        self.visible = False
        self.window.hide()
        try:
            self.window.evaluate_js("window.resetShot && window.resetShot()")
        except Exception:
            pass

    def quit(self):
        if self.hotkey:
            self.hotkey.stop()
        self.icon.stop()
        self.window.destroy()

    def _on_loaded(self):
        if not self.visible:
            self.window.hide()
        if not self.mapped:  # первая загрузка — пустая заглушка: подключаем виртуальный хост и идём на редактор
            self.mapped = True
            try:
                from System import Action
                from Microsoft.Web.WebView2.Core import CoreWebView2HostResourceAccessKind as Access
                wv = self.window.native.browser.webview

                def _go():
                    wv.CoreWebView2.SetVirtualHostNameToFolderMapping(VHOST, str(RES_DIR / "ui"), Access.Allow)
                    wv.CoreWebView2.Navigate(f"https://{VHOST}/editor.html")

                wv.Invoke(Action(_go))
            except Exception:
                log.exception("virtual host")
            return
        try:
            state = self.window.evaluate_js("location.href + ' | ' + typeof window.loadShot")
        except Exception as e:
            state = e
        self.page_ready = True
        log.info("editor page loaded: %s", state)

    def _on_closing(self):
        # Alt+F4 на оверлее — просто прячем. Из отдельного потока: обработчик идёт в UI-потоке,
        # а evaluate_js внутри hide_overlay ждёт этот же поток — было бы зависание
        threading.Thread(target=self.hide_overlay, daemon=True).start()
        return False

    def run(self):
        import webview
        self.icon.run_detached()
        self.register_hotkey()
        vx, vy, vw, vh = virtual_screen()
        self.window = webview.create_window(
            # скрытое окно WebView2 не грузит страницу — стартуем за краем экрана и прячем после загрузки
            APP_NAME, html="<body style='background:#000'></body>", js_api=Api(self),
            x=-32000, y=-32000, width=vw, height=vh,
            frameless=True, on_top=True, background_color="#000000", easy_drag=False)
        self.window.events.closing += self._on_closing
        self.window.events.loaded += self._on_loaded
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(DATA_DIR / "webview"))
        if self.hotkey:
            self.hotkey.stop()
        self.icon.stop()


def main():
    # per-monitor DPI: иначе при разном масштабе мониторов координаты и снимок не совпадают
    if not user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
        user32.SetProcessDPIAware()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=DATA_DIR / "quickshot.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")
    faulthandler.enable(open(DATA_DIR / "crash.log", "a", encoding="utf-8"))
    kernel32.CreateMutexW(None, False, "QuickShot_single_instance")
    if kernel32.GetLastError() == 183:
        user32.MessageBoxW(None, "QuickShot уже запущен — иконка в трее.", APP_NAME, 0x40)
        return
    try:
        App().run()
    except Exception:
        log.exception("fatal")
        raise


if __name__ == "__main__":
    main()
