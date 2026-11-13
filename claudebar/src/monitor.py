"""What the panel asks of Win32: the monitor under a screen point (its work area, its height and its
DPI) and a picture of a window."""

import ctypes
import functools
from ctypes import wintypes
from typing import NamedTuple, Optional

from PIL import Image

_DEFAULT_TO_NULL, _DEFAULT_TO_NEAREST = 0, 2     # MONITOR_DEFAULTTONULL / MONITOR_DEFAULTTONEAREST
_EFFECTIVE_DPI = 0                               # MDT_EFFECTIVE_DPI
_STANDARD_DPI = 96
_RENDER_FULL_CONTENT = 2                         # PW_RENDERFULLCONTENT
_EXSTYLE, _LAYERED = -20, 0x80000               # GWL_EXSTYLE, WS_EX_LAYERED
_USE_ALPHA, _OPAQUE = 2, 255                    # LWA_ALPHA, fully opaque


class Monitor(NamedTuple):
    work: tuple      # (left, top, right, bottom): the screen the taskbar leaves free
    height: int      # the whole monitor, in pixels
    dpi: int         # 96 is 100%


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


@functools.cache
def _api():
    """user32 and shcore with their signatures set. Private handles, so the argument types do
    not leak into other users of ctypes.windll; the 64-bit monitor handle is not truncated."""
    user32, shcore = ctypes.WinDLL("user32"), ctypes.WinDLL("shcore")
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.MonitorFromPoint.restype = wintypes.HMONITOR
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MonitorInfo)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    shcore.GetDpiForMonitor.argtypes = [wintypes.HMONITOR, ctypes.c_int,
                                        ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)]
    shcore.GetDpiForMonitor.restype = ctypes.c_long
    return user32, shcore


def monitor_at(x: int, y: int, nearest: bool = True) -> Optional[Monitor]:
    """The monitor holding the screen point (x, y), or the nearest one. None when `nearest` is
    off and the point is on no monitor, and when Windows cannot say."""
    try:
        user32, shcore = _api()
        handle = user32.MonitorFromPoint(wintypes.POINT(int(x), int(y)),
                                         _DEFAULT_TO_NEAREST if nearest else _DEFAULT_TO_NULL)
        info = _MonitorInfo(cbSize=ctypes.sizeof(_MonitorInfo))
        if not handle or not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            return None
        dpi_x, dpi_y = wintypes.UINT(), wintypes.UINT()
        ok = shcore.GetDpiForMonitor(handle, _EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0
        work = info.rcWork
        return Monitor((work.left, work.top, work.right, work.bottom),
                       info.rcMonitor.bottom - info.rcMonitor.top,
                       dpi_x.value if ok and dpi_x.value else _STANDARD_DPI)
    except (AttributeError, OSError, ctypes.ArgumentError):      # no Win32 here, or a call that failed
        return None


class _BitmapInfo(ctypes.Structure):
    """BITMAPINFO for 32-bit rows (no colour table is read), as GetDIBits wants it."""
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD), ("bmiColors", wintypes.DWORD)]


@functools.cache
def _window_api():
    """user32 and gdi32 with the signatures of the window calls set, on private handles like _api."""
    user32, gdi32 = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
    user32.GetParent.restype, user32.GetParent.argtypes = wintypes.HWND, [wintypes.HWND]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetDC.restype, user32.GetDC.argtypes = wintypes.HDC, [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.GetWindowLongW.argtypes, user32.GetWindowLongW.restype = [wintypes.HWND, ctypes.c_int], ctypes.c_long
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD]
    gdi32.CreateCompatibleDC.restype, gdi32.CreateCompatibleDC.argtypes = wintypes.HDC, [wintypes.HDC]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype, gdi32.SelectObject.argtypes = wintypes.HGDIOBJ, [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                ctypes.POINTER(_BitmapInfo), wintypes.UINT]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    return user32, gdi32


def window_picture(client_hwnd: int) -> Optional[Image.Image]:
    """The pixels of the top-level window that holds `client_hwnd` (Tk's winfo_id), as it draws itself:
    PrintWindow on the frame window gives its own surface, so a window over it and the desktop behind
    its rounded corners stay out. None when Windows will not give a picture."""
    try:
        user32, gdi32 = _window_api()
        frame = user32.GetParent(client_hwnd) or client_hwnd
        rect = wintypes.RECT()
        if not user32.GetWindowRect(frame, ctypes.byref(rect)):
            return None
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return None
        screen = user32.GetDC(None)
        memory = gdi32.CreateCompatibleDC(screen)
        bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
        try:
            gdi32.SelectObject(memory, bitmap)
            info = _BitmapInfo(biSize=40, biWidth=width, biHeight=-height, biPlanes=1, biBitCount=32)
            pixels = ctypes.create_string_buffer(width * height * 4)
            if not (user32.PrintWindow(frame, memory, _RENDER_FULL_CONTENT)
                    and gdi32.GetDIBits(memory, bitmap, 0, height, pixels, ctypes.byref(info), 0)):
                return None
        finally:
            gdi32.DeleteDC(memory)
            gdi32.DeleteObject(bitmap)
            user32.ReleaseDC(None, screen)
        picture = Image.frombuffer("RGBA", (width, height), pixels, "raw", "BGRA", 0, 1).convert("RGB")
        return picture if picture.getbbox() else None          # an all-black answer is no picture
    except (AttributeError, OSError, ctypes.ArgumentError, ValueError):
        return None


def make_layered(hwnd: int) -> bool:
    """Make a child window layered (Windows 8 and later): it is composed over its siblings and not cut out
    of them, so what lies under it keeps painting and is whole when it goes. False when Windows refuses."""
    try:
        user32, _ = _window_api()
        user32.SetWindowLongW(hwnd, _EXSTYLE, user32.GetWindowLongW(hwnd, _EXSTYLE) | _LAYERED)
        return bool(user32.SetLayeredWindowAttributes(hwnd, 0, _OPAQUE, _USE_ALPHA))
    except (AttributeError, OSError, ctypes.ArgumentError):
        return False

