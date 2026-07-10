# -*- coding: utf-8 -*-
"""
DdSTT — трей-приложение, которое через случайные промежутки времени
показывает GIF на весь экран с настоящей альфа-прозрачностью
(через Windows Layered Window / UpdateLayeredWindow).

Требования (Windows, Python 3.9+):
    pip install pywin32 pillow pystray

Запуск для теста:
    pythonw ddstt_tray.py      (без консоли)
    python  ddstt_tray.py      (с консолью, для отладки)

Для автозапуска с Windows приложение само добавляет себя в реестр
(HKEY_CURRENT_USER\\...\\Run) при первом запуске, либо это можно
включить/выключить через меню в трее.

Для сборки в exe см. build_exe.bat (pyinstaller).
"""

import os
import sys
import time
import ctypes
import random
import threading
import winreg
from ctypes import wintypes

from PIL import Image, ImageDraw
import win32gui
import win32con
import win32api
import win32process
import pystray

# ----------------------------- НАСТРОЙКИ -----------------------------

APP_NAME = "DdSTT"

# Путь к gif. Если приложение собрано pyinstaller-ом с --add-data,
# используем sys._MEIPASS, иначе берём файл рядом со скриптом.
def resource_path(rel_path):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel_path)

GIF_PATH = resource_path(os.path.join("assets", "DdSTT.gif"))

# Диапазон случайного интервала между показами, в секундах.
# MIN_INTERVAL_SEC — статичный минимум, ниже которого показ не наступит.
# Реальная пауза каждый раз выбирается случайно в диапазоне
# [MIN_INTERVAL_SEC, MAX_INTERVAL_SEC].
MIN_INTERVAL_SEC = 30             # 30 секунд (статично)
MAX_INTERVAL_SEC = 30 * 60        # 30 минут

# Растягивать гифку на весь экран (True) или показывать в реальном
# размере по центру экрана (False).
STRETCH_FULLSCREEN = True

# Чёрно-белый эффект на весь экран сразу после проигрывания гифки.
BW_EFFECT_ENABLED = True
BW_EFFECT_DURATION_SEC = 3.0

# ------------------------- АВТОЗАПУСК (РЕЕСТР) -------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _startup_command():
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        # заменим на pythonw.exe, чтобы не мелькала консоль
        pyw = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(pyw):
            exe = pyw
        script = os.path.abspath(__file__)
        return f'"{exe}" "{script}"'
    else:
        # уже exe (pyinstaller)
        return f'"{exe}"'


def is_autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except FileNotFoundError:
        return False


def set_autostart(enabled: bool):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _startup_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


# --------------------- ПОКАЗ GIF ЧЕРЕЗ LAYERED WINDOW ---------------------

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

ULW_ALPHA = 0x02
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte),
        ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat", ctypes.c_byte),
    ]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _load_frames(gif_path, target_w, target_h, stretch):
    """Возвращает список (premultiplied BGRA bytes, width, height, duration_ms)."""
    im = Image.open(gif_path)
    frames = []
    try:
        while True:
            frame = im.convert("RGBA")

            if stretch:
                w, h = target_w, target_h
                frame = frame.resize((w, h), Image.LANCZOS)
            else:
                w, h = frame.size

            duration = im.info.get("duration", 100)
            if duration <= 0:
                duration = 100

            # Premultiply alpha и перевод RGBA -> BGRA (нужно для GDI)
            px = frame.load()
            buf = bytearray(w * h * 4)
            idx = 0
            data = frame.tobytes()
            # data идёт как R,G,B,A по строкам — обрабатываем массово через bytearray
            arr = bytearray(data)
            for i in range(0, len(arr), 4):
                r, g, b, a = arr[i], arr[i + 1], arr[i + 2], arr[i + 3]
                buf[i] = (b * a) // 255
                buf[i + 1] = (g * a) // 255
                buf[i + 2] = (r * a) // 255
                buf[i + 3] = a
            frames.append((bytes(buf), w, h, duration))

            im.seek(im.tell() + 1)
    except EOFError:
        pass
    return frames


class GifOverlay:
    """Полноэкранное layered-окно, проигрывающее один раз GIF и закрывающееся."""

    def __init__(self):
        self.hwnd = None
        self.class_atom = None

    def play_blocking(self):
        """Показывает гиф и блокирует поток до окончания проигрывания."""
        screen_w = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        screen_h = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)

        frames = _load_frames(GIF_PATH, screen_w, screen_h, STRETCH_FULLSCREEN)
        if not frames:
            return

        if STRETCH_FULLSCREEN:
            win_w, win_h = screen_w, screen_h
            win_x, win_y = 0, 0
        else:
            win_w, win_h = frames[0][1], frames[0][2]
            win_x = (screen_w - win_w) // 2
            win_y = (screen_h - win_h) // 2

        hinst = win32api.GetModuleHandle(None)
        class_name = "DdSTTOverlayWindowClass"

        wc = win32gui.WNDCLASS()
        wc.hInstance = hinst
        wc.lpszClassName = class_name
        wc.lpfnWndProc = {win32con.WM_DESTROY: lambda hwnd, msg, wp, lp: win32gui.PostQuitMessage(0)}
        try:
            win32gui.RegisterClass(wc)
        except Exception:
            pass  # класс уже зарегистрирован с прошлого раза

        ex_style = (win32con.WS_EX_LAYERED |
                    win32con.WS_EX_TOPMOST |
                    win32con.WS_EX_TOOLWINDOW |
                    win32con.WS_EX_TRANSPARENT)
        style = win32con.WS_POPUP

        hwnd = win32gui.CreateWindowEx(
            ex_style, class_name, APP_NAME, style,
            win_x, win_y, win_w, win_h,
            0, 0, hinst, None
        )
        self.hwnd = hwnd

        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)

        screen_dc = win32gui.GetDC(0)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)

        try:
            for frame_bytes, w, h, duration_ms in frames:
                bmi = self._make_bitmapinfo(w, h)
                bits_ptr = ctypes.c_void_p()
                hbitmap = gdi32.CreateDIBSection(
                    mem_dc, ctypes.byref(bmi), 0,
                    ctypes.byref(bits_ptr), None, 0
                )
                ctypes.memmove(bits_ptr, frame_bytes, len(frame_bytes))

                old_bmp = gdi32.SelectObject(mem_dc, hbitmap)

                size = SIZE(w, h)
                pt_src = POINT(0, 0)
                pt_dst = POINT(win_x, win_y)
                blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)

                user32.UpdateLayeredWindow(
                    hwnd, screen_dc,
                    ctypes.byref(pt_dst), ctypes.byref(size),
                    mem_dc, ctypes.byref(pt_src),
                    0, ctypes.byref(blend), ULW_ALPHA
                )

                gdi32.SelectObject(mem_dc, old_bmp)
                gdi32.DeleteObject(hbitmap)

                # Прокачиваем очередь сообщений, чтобы окно не "зависло",
                # и позволяем закрыть заранее правым кликом (не реализовано,
                # но цикл сообщений держим отзывчивым).
                self._pump_messages()
                time.sleep(duration_ms / 1000.0)
        finally:
            gdi32.DeleteDC(mem_dc)
            win32gui.ReleaseDC(0, screen_dc)
            win32gui.DestroyWindow(hwnd)
            self._pump_messages()

    @staticmethod
    def _pump_messages():
        while True:
            has_msg, msg = win32gui.PeekMessage(0, 0, 0, win32con.PM_REMOVE)
            if not has_msg:
                break
            win32gui.TranslateMessage(msg)
            win32gui.DispatchMessage(msg)

    @staticmethod
    def _make_bitmapinfo(w, h):
        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = w
        bmi.biHeight = -h  # отрицательная высота = top-down bitmap
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0  # BI_RGB
        return bmi


class BWOverlay:
    """Полноэкранный визуальный оверлей: снимок рабочего стола в ч/б,
    показывается на BW_EFFECT_DURATION_SEC секунд и закрывается.

    Сделан через тот же механизм, что и GifOverlay (WS_EX_LAYERED +
    UpdateLayeredWindow) — это единственный надёжный способ добиться
    настоящего click-through в Windows. Одного WS_EX_TRANSPARENT без
    WS_EX_LAYERED недостаточно: клики/движения мыши всё равно частично
    перехватываются окном, из-за чего экран "подвисает" на вид."""

    def play_blocking(self, duration_sec):
        from PIL import ImageGrab

        screen_w = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        screen_h = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)

        try:
            shot = ImageGrab.grab(bbox=(0, 0, screen_w, screen_h))
        except Exception:
            return

        gray_rgba = shot.convert("L").convert("RGBA")
        w, h = gray_rgba.size

        # Полностью непрозрачные пиксели (A=255) -> premultiply не меняет
        # значения, но переводим RGBA -> BGRA, как требует GDI.
        arr = bytearray(gray_rgba.tobytes())
        buf = bytearray(w * h * 4)
        for i in range(0, len(arr), 4):
            r, g, b, a = arr[i], arr[i + 1], arr[i + 2], arr[i + 3]
            buf[i] = b
            buf[i + 1] = g
            buf[i + 2] = r
            buf[i + 3] = 255

        hinst = win32api.GetModuleHandle(None)
        class_name = "DdSTTBWOverlayWindowClass"

        wc = win32gui.WNDCLASS()
        wc.hInstance = hinst
        wc.lpszClassName = class_name
        wc.lpfnWndProc = {win32con.WM_DESTROY: lambda hwnd, msg, wp, lp: win32gui.PostQuitMessage(0)}
        try:
            win32gui.RegisterClass(wc)
        except Exception:
            pass

        ex_style = (win32con.WS_EX_LAYERED |
                    win32con.WS_EX_TOPMOST |
                    win32con.WS_EX_TOOLWINDOW |
                    win32con.WS_EX_TRANSPARENT |   # клики проходят "сквозь" окно
                    win32con.WS_EX_NOACTIVATE)     # не перехватывает фокус
        style = win32con.WS_POPUP

        hwnd = win32gui.CreateWindowEx(
            ex_style, class_name, APP_NAME, style,
            0, 0, screen_w, screen_h,
            0, 0, hinst, None
        )
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)

        screen_dc = win32gui.GetDC(0)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)

        bmi = GifOverlay._make_bitmapinfo(w, h)
        bits_ptr = ctypes.c_void_p()
        hbitmap = gdi32.CreateDIBSection(
            mem_dc, ctypes.byref(bmi), 0,
            ctypes.byref(bits_ptr), None, 0
        )
        ctypes.memmove(bits_ptr, bytes(buf), len(buf))
        old_bmp = gdi32.SelectObject(mem_dc, hbitmap)

        size = SIZE(w, h)
        pt_src = POINT(0, 0)
        pt_dst = POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)

        user32.UpdateLayeredWindow(
            hwnd, screen_dc,
            ctypes.byref(pt_dst), ctypes.byref(size),
            mem_dc, ctypes.byref(pt_src),
            0, ctypes.byref(blend), ULW_ALPHA
        )

        try:
            end_time = time.time() + duration_sec
            while time.time() < end_time:
                GifOverlay._pump_messages()
                time.sleep(0.05)
        finally:
            gdi32.SelectObject(mem_dc, old_bmp)
            gdi32.DeleteObject(hbitmap)
            gdi32.DeleteDC(mem_dc)
            win32gui.ReleaseDC(0, screen_dc)
            win32gui.DestroyWindow(hwnd)
            GifOverlay._pump_messages()


# ------------------------------ ЛОГИКА ------------------------------

class App:
    def __init__(self):
        self.enabled = True
        self.stop_event = threading.Event()
        self.force_event = threading.Event()
        self.play_lock = threading.Lock()
        self.next_run_at = time.time() + MIN_INTERVAL_SEC  # для таймера в трее

    def _worker(self):
        while not self.stop_event.is_set():
            wait_time = random.uniform(MIN_INTERVAL_SEC, MAX_INTERVAL_SEC)
            self.next_run_at = time.time() + wait_time

            elapsed = 0.0
            step = 0.5
            while elapsed < wait_time and not self.stop_event.is_set():
                if self.force_event.is_set():
                    break
                time.sleep(step)
                elapsed += step

            if self.stop_event.is_set():
                break

            self.force_event.clear()

            if self.enabled:
                self._play_gif()
            # цикл начинается заново -> таймер сбрасывается

    def _play_gif(self):
        # Если показ уже идёт (например, предыдущий Force Run ещё не
        # закончился) — просто игнорируем повторный вызов, не ставим в очередь.
        if not self.play_lock.acquire(blocking=False):
            return
        try:
            GifOverlay().play_blocking()
            if BW_EFFECT_ENABLED:
                BWOverlay().play_blocking(BW_EFFECT_DURATION_SEC)
        except Exception as e:
            # не роняем фоновый поток из-за ошибки показа
            sys.stderr.write(f"[DdSTT] Ошибка показа GIF: {e}\n")
        finally:
            self.play_lock.release()

    def force_run(self):
        # Показ по требованию — единожды, не дожидаясь таймера, в отдельном
        # потоке (чтобы не блокировать трей). Повторное нажатие после
        # завершения показа снова запускает гифку с нуля.
        threading.Thread(target=self._play_gif, daemon=True).start()
        self.force_event.set()

    def toggle_enabled(self):
        self.enabled = not self.enabled

    def seconds_left(self):
        return max(0, int(self.next_run_at - time.time()))

    def start(self):
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()


def format_mmss(total_seconds):
    m, s = divmod(int(total_seconds), 60)
    return f"{m:02d}:{s:02d}"


def make_tray_icon_image():
    """Рисуем собственную заметную иконку (первый кадр гифки часто
    почти прозрачный/белый и в трее его не видно)."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # тёмный фон-кружок
    d.ellipse((2, 2, size - 2, size - 2), fill=(25, 25, 30, 255), outline=(90, 90, 100, 255), width=2)

    # простое "лицо-призрак": глаза + улыбка, ярко-фиолетовый акцент
    accent = (150, 60, 220, 255)
    eye_w, eye_h = 8, 12
    d.ellipse((18, 22, 18 + eye_w, 22 + eye_h), fill=accent)
    d.ellipse((38, 22, 38 + eye_w, 22 + eye_h), fill=accent)
    d.arc((16, 30, 48, 52), start=200, end=340, fill=accent, width=4)

    return img


def main():
    app = App()
    app.start()

    # При первом запуске включаем автозапуск по умолчанию.
    if not is_autostart_enabled():
        try:
            set_autostart(True)
        except Exception as e:
            sys.stderr.write(f"[DdSTT] Не удалось включить автозапуск: {e}\n")

    icon_image = make_tray_icon_image()

    def on_force_run(icon, item):
        app.force_run()

    def on_toggle_enabled(icon, item):
        app.toggle_enabled()

    def get_enabled_text(item):
        return "Включено ✓" if app.enabled else "Включено"

    def on_toggle_autostart(icon, item):
        set_autostart(not is_autostart_enabled())

    def get_autostart_text(item):
        return "Автозапуск ✓" if is_autostart_enabled() else "Автозапуск"

    def get_timer_text(item):
        if not app.enabled:
            return "Показы приостановлены"
        return f"След. показ через: {format_mmss(app.seconds_left())}"

    def on_exit(icon, item):
        app.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(get_timer_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Force run (проверка)", on_force_run),
        pystray.MenuItem(get_enabled_text, on_toggle_enabled),
        pystray.MenuItem(get_autostart_text, on_toggle_autostart),
        pystray.MenuItem("Выход", on_exit),
    )

    tray_icon = pystray.Icon(APP_NAME, icon_image, APP_NAME, menu)

    def timer_ticker():
        # Обновляем всплывающую подсказку (title) раз в секунду —
        # так таймер видно прямо наведя мышь на иконку в трее,
        # а пункт меню обновится при каждом открытии меню.
        while not app.stop_event.is_set():
            try:
                if app.enabled:
                    tray_icon.title = f"{APP_NAME} — след. показ через {format_mmss(app.seconds_left())}"
                else:
                    tray_icon.title = f"{APP_NAME} — на паузе"
                tray_icon.update_menu()
            except Exception:
                pass
            time.sleep(1)

    threading.Thread(target=timer_ticker, daemon=True).start()

    tray_icon.run()


if __name__ == "__main__":
    main()
