"""Capture the Windows virtual desktop and crop without resampling."""
import base64
import ctypes
import io
import sys
from ctypes import wintypes
from PIL import ImageGrab


def list_monitors():
    """Return physical-pixel monitor bounds in a stable order, primary first."""
    user32 = ctypes.windll.user32
    class MonitorInfo(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('monitor', wintypes.RECT),
                    ('work', wintypes.RECT), ('flags', wintypes.DWORD)]
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                       ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    monitors = []
    @callback_type
    def collect(handle, dc, rect, data):
        info = MonitorInfo()
        info.size = ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            return False
        r = info.monitor
        w = info.work
        monitors.append({'left': r.left, 'top': r.top, 'width': r.right-r.left,
                         'height': r.bottom-r.top, 'primary': bool(info.flags & 1),
                         'work': {'left': w.left, 'top': w.top, 'width': w.right-w.left,
                                  'height': w.bottom-w.top}})
        return True
    user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), callback_type, wintypes.LPARAM]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    if not user32.EnumDisplayMonitors(None, None, collect, 0) or not monitors:
        raise ValueError('无法获取显示器列表')
    monitors.sort(key=lambda m: (not m['primary'], m['left'], m['top']))
    for index, monitor in enumerate(monitors, 1):
        monitor['id'] = index
    return monitors


def capture_desktop(include_data=True):
    if sys.platform != 'win32':
        raise ValueError('目前仅支持 Windows 截图')
    user32 = ctypes.windll.user32
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4 & ((1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1)))
    try:
        left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        width, height = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        image = ImageGrab.grab(all_screens=True, include_layered_windows=True)
        if image.size != (width, height) or width < 1 or height < 1:
            raise ValueError('屏幕尺寸不匹配，请检查显示器缩放设置')
        info = {'left': left, 'top': top, 'width': width, 'height': height, 'monitors': list_monitors()}
        if include_data:
            output = io.BytesIO()
            image.save(output, format='PNG')
            info['image'] = 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode('ascii')
        return image, info
    except OSError as error:
        raise ValueError(f'无法截取屏幕：{error}') from error
    finally:
        if previous:
            user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(previous))


def crop_image(image, box):
    if not isinstance(box, list) or len(box) != 4 or any(type(v) is not int for v in box):
        raise ValueError('截图区域无效')
    left, top, right, bottom = box
    if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height):
        raise ValueError('截图区域无效')
    if (right - left) * (bottom - top) > 40_000_000:
        raise ValueError('截图超过 4000 万像素')
    return image.crop((left, top, right, bottom))
