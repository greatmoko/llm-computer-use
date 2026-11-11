"""Windows desktop indicator and physical double-Escape emergency signal.

Run as a separate child process so the UI and keyboard hook can be cleaned up
independently. The parent must stop the task if this process exits unexpectedly.
"""
import ctypes
import sys
import threading
import time
from ctypes import wintypes
from screenshot_capture import list_monitors


class DoubleEscape:
    def __init__(self, interval=1.0):
        self.interval = interval
        self.down = False
        self.last = None

    def key(self, is_down, injected, now):
        if injected:
            return False
        if not is_down:
            self.down = False
            return False
        if self.down:
            return False
        self.down = True
        if self.last is not None and 0 <= now - self.last <= self.interval:
            self.last = None
            return True
        self.last = now
        return False


def keep_overlay_visible(user, handles, bounds):
    """Restore windows minimized by Show Desktop and keep them above other windows."""
    left, top, width, height = bounds
    for hwnd in handles:
        if not user.IsWindow(hwnd):
            raise RuntimeError("屏幕提示窗口意外关闭")
        if user.IsIconic(hwnd) or not user.IsWindowVisible(hwnd):
            user.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
        if not user.SetWindowPos(hwnd, wintypes.HWND(-1), left, top, width, height, 0x0010 | 0x0200):
            raise RuntimeError("无法保持屏幕提示窗口置顶")


def overlay_bounds(monitor):
    """Avoid full-screen windows that cover or suppress the Windows taskbar."""
    work = monitor['work']
    bounds = tuple(work[key] for key in ('left', 'top', 'width', 'height'))
    if bounds[2] <= 0 or bounds[3] <= 0:
        raise RuntimeError("无法获取显示器工作区域")
    return bounds


def main():
    if sys.platform != "win32":
        raise RuntimeError("电脑操作提示仅支持 Windows")
    import tkinter as tk

    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel.GetModuleHandleW.restype = wintypes.HMODULE
    user.GetParent.argtypes = [wintypes.HWND]
    user.GetParent.restype = wintypes.HWND
    user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    user.SetWindowDisplayAffinity.restype = wintypes.BOOL
    user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user.SetWindowPos.restype = wintypes.BOOL
    user.IsWindow.argtypes = [wintypes.HWND]
    user.IsWindow.restype = wintypes.BOOL
    user.IsIconic.argtypes = [wintypes.HWND]
    user.IsIconic.restype = wintypes.BOOL
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsWindowVisible.restype = wintypes.BOOL
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))

    root = tk.Tk()
    root.withdraw()
    overlays = []
    for monitor in list_monitors():
        left, top, width, height = overlay_bounds(monitor)
        shade = tk.Toplevel(root)
        indicator = tk.Toplevel(root)
        for window in (shade, indicator):
            window.withdraw()
            window.overrideredirect(True)
            window.attributes("-topmost", True)
            window.geometry(f"{width}x{height}+0+0")
        shade.attributes("-alpha", .10)
        shade.configure(bg="#755cf2")
        indicator.attributes("-transparentcolor", "#010203")
        indicator.attributes("-alpha", .92)
        indicator.configure(bg="#010203")
        canvas = tk.Canvas(indicator, bg="#010203", highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)
        canvas.create_rectangle(3, 3, width - 4, height - 4, outline="#917cff", width=6)
        label = tk.Label(indicator, text="●  LLM-Computer-Use 正在操作  ·  连按两次 Esc 停止", bg="#5743c5", fg="white", font=("Microsoft YaHei UI", 12, "bold"), padx=18, pady=7)
        canvas.create_window(width // 2, 20, anchor="n", window=label)
        root.update_idletasks()
        handles = []
        for window in (shade, indicator):
            hwnd = user.GetParent(window.winfo_id())
            if not hwnd:
                raise RuntimeError("无法创建屏幕提示窗口")
            exstyle = user.GetWindowLongPtrW(hwnd, -20)
            user.SetWindowLongPtrW(hwnd, -20, exstyle | 0x00080000 | 0x20 | 0x08000000 | 0x80)
            if not user.SetWindowDisplayAffinity(hwnd, 0x11):
                raise RuntimeError("屏幕提示无法从截图中排除")
            handles.append(hwnd)
        overlays.append(((shade, indicator), handles, (left, top, width, height)))

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                    ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]

    callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    user.SetWindowsHookExW.argtypes = [ctypes.c_int, callback_type, wintypes.HMODULE, wintypes.DWORD]
    user.SetWindowsHookExW.restype = wintypes.HANDLE
    user.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user.CallNextHookEx.restype = ctypes.c_ssize_t
    user.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
    detector = DoubleEscape()

    @callback_type
    def on_key(code, message, data):
        if code >= 0 and message in (0x100, 0x101, 0x104, 0x105):
            item = ctypes.cast(data, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if item.vkCode == 0x1b and detector.key(message in (0x100, 0x104), bool(item.flags & 0x10), time.monotonic()):
                try:
                    print("STOP", flush=True)
                except OSError:
                    pass
        return user.CallNextHookEx(None, code, message, data)

    hook = user.SetWindowsHookExW(13, on_key, kernel.GetModuleHandleW(None), 0)
    if not hook:
        raise RuntimeError("无法注册 Esc 紧急停止")
    closed = threading.Event()

    def watch_parent():
        try:
            sys.stdin.buffer.read()
        finally:
            closed.set()

    threading.Thread(target=watch_parent, daemon=True).start()
    def poll_parent():
        if closed.is_set():
            root.destroy()
        else:
            try:
                for _, handles, bounds in overlays:
                    keep_overlay_visible(user, handles, bounds)
            except RuntimeError as error:
                print(str(error), file=sys.stderr, flush=True)
                root.destroy()
                return
            root.after(250, poll_parent)
    try:
        for windows, _, _ in overlays:
            for window in windows:
                window.deiconify()
        root.update()
        for _, handles, bounds in overlays:
            keep_overlay_visible(user, handles, bounds)
        print("READY", flush=True)
        root.after(100, poll_parent)
        root.mainloop()
    finally:
        user.UnhookWindowsHookEx(hook)
        try:
            root.destroy()
        except tk.TclError:
            pass


if __name__ == "__main__":
    main()
