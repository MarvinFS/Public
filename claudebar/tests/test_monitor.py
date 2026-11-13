"""Which monitor a point is on, read from Win32 through a fake: its work area, its height and its DPI,
and what comes back when Windows cannot say. And a picture of a window, and a layered child window."""

import ctypes

import pytest

import monitor


class FakeUser32:
    def __init__(self, handle=7, work=(0, 0, 1920, 1032), full=(0, 0, 1920, 1080), info_ok=True):
        self.handle, self.work, self.full, self.info_ok = handle, work, full, info_ok
        self.flags = []

    def MonitorFromPoint(self, point, flags):
        self.flags.append((point.x, point.y, flags))
        return self.handle

    def GetMonitorInfoW(self, handle, ref):
        info = ref._obj
        (info.rcMonitor.left, info.rcMonitor.top, info.rcMonitor.right, info.rcMonitor.bottom) = self.full
        (info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom) = self.work
        return 1 if self.info_ok else 0


class FakeShcore:
    def __init__(self, dpi=144, result=0):
        self.dpi, self.result = dpi, result

    def GetDpiForMonitor(self, handle, kind, dpi_x, dpi_y):
        dpi_x._obj.value = dpi_y._obj.value = self.dpi
        return self.result


@pytest.fixture
def win32(monkeypatch):
    def install(user32=None, shcore=None):
        user32, shcore = user32 or FakeUser32(), shcore or FakeShcore()
        monkeypatch.setattr(monitor, "_api", lambda: (user32, shcore))
        return user32, shcore
    return install


def test_a_monitor_has_its_work_area_its_height_and_its_dpi(win32):
    win32(FakeUser32(work=(0, 0, 1920, 1032), full=(0, 0, 1920, 1080)), FakeShcore(dpi=144))
    assert monitor.monitor_at(100, 100) == monitor.Monitor((0, 0, 1920, 1032), 1080, 144)


def test_the_height_is_the_whole_monitors_and_not_the_work_areas(win32):
    win32(FakeUser32(work=(2560, 0, 5120, 1392), full=(2560, 0, 5120, 1440)))
    found = monitor.monitor_at(3000, 10)
    assert found.height == 1440 and found.work[3] == 1392 and found.work[0] == 2560


def test_the_nearest_monitor_is_asked_for_unless_the_point_must_be_on_one(win32):
    user32, _ = win32()
    monitor.monitor_at(5, 6)
    monitor.monitor_at(7, 8, nearest=False)
    assert user32.flags == [(5, 6, 2), (7, 8, 0)]      # MONITOR_DEFAULTTONEAREST, MONITOR_DEFAULTTONULL


def test_a_point_on_no_monitor_is_none_when_it_must_be_on_one(win32):
    win32(FakeUser32(handle=0))
    assert monitor.monitor_at(-100_000, 0, nearest=False) is None


@pytest.mark.parametrize("shcore", [FakeShcore(result=0x80070057), FakeShcore(dpi=0)])
def test_a_dpi_that_cannot_be_read_is_96(win32, shcore):
    win32(shcore=shcore)
    assert monitor.monitor_at(0, 0).dpi == 96


def test_a_monitor_whose_info_cannot_be_read_is_none(win32):
    win32(FakeUser32(info_ok=False))
    assert monitor.monitor_at(0, 0) is None


@pytest.mark.parametrize("error", [AttributeError("no WinDLL here"), OSError("no such dll"),
                                   ctypes.ArgumentError("bad argument")])
def test_when_windows_cannot_say_it_is_none_and_never_an_exception(monkeypatch, error):
    def broken():
        raise error
    monkeypatch.setattr(monitor, "_api", broken)
    assert monitor.monitor_at(0, 0) is None
    assert monitor.monitor_at(0, 0, nearest=False) is None


class FakePictureUser32:
    def __init__(self, parent=99, rect=(10, 20, 14, 23), printed=True, layered_ok=1, style=0x40):
        self.parent, self.rect, self.printed, self.layered_ok, self.style = parent, rect, printed, layered_ok, style
        self.log = []

    def GetParent(self, hwnd):
        self.log.append(("GetParent", hwnd))
        return self.parent

    def GetWindowRect(self, hwnd, ref):
        self.log.append(("GetWindowRect", hwnd))
        (ref._obj.left, ref._obj.top, ref._obj.right, ref._obj.bottom) = self.rect
        return 1

    def GetDC(self, hwnd):
        return 5

    def ReleaseDC(self, hwnd, dc):
        self.log.append("ReleaseDC")

    def PrintWindow(self, hwnd, dc, flags):
        self.log.append(("PrintWindow", hwnd, flags))
        return 1 if self.printed else 0

    def GetWindowLongW(self, hwnd, index):
        self.log.append(("GetWindowLongW", hwnd, index))
        return self.style

    def SetWindowLongW(self, hwnd, index, value):
        self.log.append(("SetWindowLongW", hwnd, index, value))

    def SetLayeredWindowAttributes(self, hwnd, key, alpha, flags):
        self.log.append(("SetLayeredWindowAttributes", hwnd, alpha, flags))
        return self.layered_ok


class FakeGdi32:
    def __init__(self, pixel=b"\x01\x02\x03\xff"):
        self.pixel = pixel
        self.log = []

    def CreateCompatibleDC(self, dc):
        return 6

    def CreateCompatibleBitmap(self, dc, width, height):
        return 7

    def SelectObject(self, dc, obj):
        return 8

    def GetDIBits(self, dc, bitmap, first, lines, pixels, info, usage):
        data = self.pixel * (len(pixels) // len(self.pixel))
        ctypes.memmove(pixels, data, len(data))
        return lines

    def DeleteDC(self, dc):
        self.log.append("DeleteDC")

    def DeleteObject(self, obj):
        self.log.append("DeleteObject")


@pytest.fixture
def picture_api(monkeypatch):
    def install(user32=None, gdi32=None):
        user32, gdi32 = user32 or FakePictureUser32(), gdi32 or FakeGdi32()
        monkeypatch.setattr(monitor, "_window_api", lambda: (user32, gdi32))
        return user32, gdi32
    return install


def test_a_window_picture_is_the_windows_own_pixels_as_rgb(picture_api):
    picture_api()
    picture = monitor.window_picture(42)
    assert picture.mode == "RGB" and picture.size == (4, 3)
    assert picture.getpixel((0, 0)) == picture.getpixel((3, 2)) == (3, 2, 1)       # the surface is BGRA


def test_the_frame_window_is_printed_with_the_full_content_flag(picture_api):
    user32, _ = picture_api()
    monitor.window_picture(42)
    assert ("GetWindowRect", 99) in user32.log and ("PrintWindow", 99, 2) in user32.log


def test_a_window_with_no_parent_is_printed_itself(picture_api):
    user32, _ = picture_api(FakePictureUser32(parent=0))
    monitor.window_picture(42)
    assert ("PrintWindow", 42, 2) in user32.log


def test_when_windows_will_not_print_there_is_no_picture_and_nothing_leaks(picture_api):
    user32, gdi32 = picture_api(FakePictureUser32(printed=False))
    assert monitor.window_picture(42) is None
    assert "ReleaseDC" in user32.log and gdi32.log == ["DeleteDC", "DeleteObject"]


@pytest.mark.parametrize("rect", [(0, 0, 0, 0), (5, 5, 5, 40), (5, 5, 40, 5), (9, 9, 4, 4)])
def test_a_window_with_no_area_has_no_picture(picture_api, rect):
    picture_api(FakePictureUser32(rect=rect))
    assert monitor.window_picture(42) is None


def test_an_all_black_answer_is_no_picture(picture_api):
    picture_api(gdi32=FakeGdi32(pixel=b"\x00\x00\x00\x00"))
    assert monitor.window_picture(42) is None


@pytest.mark.parametrize("error", [AttributeError("no WinDLL here"), OSError("no such dll"),
                                   ctypes.ArgumentError("bad argument")])
def test_when_windows_cannot_print_it_is_none_and_never_an_exception(monkeypatch, error):
    def broken():
        raise error
    monkeypatch.setattr(monitor, "_window_api", broken)
    assert monitor.window_picture(42) is None


def test_a_layered_window_keeps_its_style_and_is_fully_opaque(picture_api):
    user32, _ = picture_api()
    assert monitor.make_layered(42) is True
    assert ("SetWindowLongW", 42, -20, 0x40 | 0x80000) in user32.log
    assert ("SetLayeredWindowAttributes", 42, 255, 2) in user32.log


def test_a_window_windows_will_not_layer_says_so(picture_api):
    picture_api(FakePictureUser32(layered_ok=0))
    assert monitor.make_layered(42) is False


@pytest.mark.parametrize("error", [AttributeError("no WinDLL here"), OSError("no such dll"),
                                   ctypes.ArgumentError("bad argument")])
def test_when_windows_cannot_layer_it_is_false_and_never_an_exception(monkeypatch, error):
    def broken():
        raise error
    monkeypatch.setattr(monitor, "_window_api", broken)
    assert monitor.make_layered(42) is False
