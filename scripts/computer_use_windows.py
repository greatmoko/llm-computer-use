"""Windows desktop input primitives for computer-use batches.

No agent or network endpoint is registered here. Callers must enforce task
authorization and arrange a visible indicator before using this driver.
"""
import ctypes
import codecs
import io
import locale
import math
import os
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes
from PIL import Image, ImageDraw

from computer_use_batch import BatchStopped
from screenshot_capture import capture_desktop, crop_image
from winjob import ProcessJob


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", wintypes.DWORD), ("data", INPUT_UNION)]


KEYS = {"BACKSPACE": 0x08, "TAB": 0x09, "ENTER": 0x0D, "SHIFT": 0x10,
        "CTRL": 0x11, "ALT": 0x12, "ESC": 0x1B, "SPACE": 0x20,
        "PAGEUP": 0x21, "PAGEDOWN": 0x22, "END": 0x23, "HOME": 0x24,
        "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
        "INSERT": 0x2D, "DELETE": 0x2E, "WIN": 0x5B}
KEYS.update({chr(c): c for c in range(ord("A"), ord("Z") + 1)})
KEYS.update({str(n): ord(str(n)) for n in range(10)})
KEYS.update({f"F{n}": 0x6F + n for n in range(1, 25)})
MOUSE_FLAGS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}


def decode_command_output(data):
    truncated = len(data) > 32_768
    raw = bytes(data[:32_768])
    encodings = ["utf-8", locale.getencoding(), "gbk"]
    if sys.platform == "win32":
        codepage = ctypes.windll.kernel32.GetConsoleOutputCP()
        if codepage:
            encodings.insert(1, f"cp{codepage}")
    for encoding in dict.fromkeys(encodings):
        try:
            value = codecs.getincrementaldecoder(encoding)(errors="strict").decode(raw, final=not truncated)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        value = raw.decode("utf-8", errors="replace")
    return value + ("\n…输出已截断" if truncated else "")


class WindowsDesktopDriver:
    def __init__(self, move_duration=0.1):
        if sys.platform != "win32":
            raise OSError("电脑操作目前仅支持 Windows")
        if not 0.05 <= move_duration <= 5:
            raise ValueError("鼠标移动时间无效")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        self.user32.SendInput.restype = wintypes.UINT
        self.user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user32.GetWindowRect.restype = wintypes.BOOL
        self.user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        self.user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        self.move_duration = move_duration
        self.pressed_keys = []
        self.pressed_buttons = []
        self._job = None
        self._job_lock = threading.Lock()
        self.last_image = None

    def interrupt(self):
        with self._job_lock:
            if self._job is not None:
                self._job.terminate(0)

    def _send(self, event):
        if self.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT)) != 1:
            raise OSError("无法发送输入；目标窗口可能受 Windows 权限限制")

    def _mouse(self, flags, data=0):
        event = INPUT(type=0)
        event.mi = MOUSEINPUT(0, 0, data & 0xffffffff, flags, 0, 0)
        self._send(event)

    def _key(self, key, down):
        event = INPUT(type=1)
        event.ki = KEYBDINPUT(key, 0, 0 if down else 0x0002, 0, 0)
        self._send(event)

    @staticmethod
    def _check(stop):
        if stop.is_set():
            raise BatchStopped()

    def _bounds(self, x, y):
        left, top = self.user32.GetSystemMetrics(76), self.user32.GetSystemMetrics(77)
        width, height = self.user32.GetSystemMetrics(78), self.user32.GetSystemMetrics(79)
        if width <= 0 or height <= 0 or not (left <= x < left + width and top <= y < top + height):
            raise ValueError("坐标超出桌面范围")

    def _image_coordinates(self, action):
        if "imageId" not in action:
            return action
        image = self.last_image
        if not image or action["imageId"] != image["imageId"]:
            raise ValueError("截图已过期或不存在，请重新截图")
        width, height = action.get("imageSize", [image.get("imageWidth", image["width"]), image.get("imageHeight", image["height"])])
        result = dict(action)
        for xkey, ykey in (("x", "y"), ("endX", "endY")):
            if xkey not in result:
                continue
            x, y = result[xkey], result[ykey]
            if not 0 <= x < width or not 0 <= y < height:
                raise ValueError("坐标超出截图范围")
            result[xkey] = image["left"] + x * image["width"] // width
            result[ykey] = image["top"] + y * image["height"] // height
        return result

    def _active_region(self, info):
        user = self.user32
        user.GetForegroundWindow.restype = wintypes.HWND
        hwnd = user.GetForegroundWindow()
        rect = wintypes.RECT()
        name = ctypes.create_unicode_buffer(256)
        if hwnd:
            user.GetClassNameW(hwnd, name, len(name))
        if hwnd and name.value not in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd") and user.GetWindowRect(hwnd, ctypes.byref(rect)):
            left = max(rect.left, info["left"])
            top = max(rect.top, info["top"])
            right = min(rect.right, info["left"] + info["width"])
            bottom = min(rect.bottom, info["top"] + info["height"])
            if right > left and bottom > top:
                return left, top, right-left, bottom-top, "window"
        point = POINT()
        if not user.GetCursorPos(ctypes.byref(point)):
            raise OSError("无法获取当前屏幕")
        monitor = next((m for m in info["monitors"] if m["left"] <= point.x < m["left"]+m["width"] and m["top"] <= point.y < m["top"]+m["height"]), None)
        if monitor is None:
            monitor = next(m for m in info["monitors"] if m["primary"])
        return *(monitor[k] for k in ("left", "top", "width", "height")), "monitor"

    def _move(self, x, y, stop):
        self._bounds(x, y)
        point = POINT()
        if not self.user32.GetCursorPos(ctypes.byref(point)):
            raise OSError("无法读取鼠标位置")
        count = max(1, math.ceil(self.move_duration / .01))
        start = time.monotonic()
        for index in range(1, count + 1):
            self._check(stop)
            fraction = index / count
            smooth = fraction * fraction * (3 - 2 * fraction)
            if not self.user32.SetCursorPos(round(point.x + (x - point.x) * smooth), round(point.y + (y - point.y) * smooth)):
                raise OSError("无法移动鼠标")
            if stop.wait(max(0, start + self.move_duration * fraction - time.monotonic())):
                raise BatchStopped()

    def _button(self, button, down):
        self._mouse(MOUSE_FLAGS[button][0 if down else 1])
        if down:
            self.pressed_buttons.append(button)
        elif button in self.pressed_buttons:
            self.pressed_buttons.remove(button)

    def _keys(self, keys, state, stop):
        codes = []
        for key in keys:
            code = KEYS.get(key.upper())
            if code is None:
                raise ValueError(f"不支持的按键：{key}")
            codes.append(code)
        if len(set(codes)) != len(codes):
            raise ValueError("按键不能重复")
        if state in ("press", "down"):
            for code in codes:
                self._check(stop)
                if code in self.pressed_keys:
                    raise ValueError("按键已按下")
                self._key(code, True)
                self.pressed_keys.append(code)
                if len(codes) > 1 and stop.wait(.03):
                    raise BatchStopped()
        if state in ("press", "up"):
            for code in reversed(codes):
                self._check(stop)
                if code not in self.pressed_keys:
                    raise ValueError("按键未由任务按下")
                self._key(code, False)
                self.pressed_keys.remove(code)
                if len(codes) > 1 and stop.wait(.03):
                    raise BatchStopped()

    def _clipboard_write(self, value):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HANDLE
        kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
        kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
        self.user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        self.user32.SetClipboardData.restype = wintypes.HANDLE
        raw = (value + "\0").encode("utf-16-le")
        handle = kernel32.GlobalAlloc(0x0002, len(raw))
        if not handle:
            raise OSError("无法分配剪贴板内存")
        opened = False
        try:
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                raise OSError("无法写入剪贴板内存")
            ctypes.memmove(pointer, raw, len(raw))
            kernel32.GlobalUnlock(handle)
            if not self.user32.OpenClipboard(None):
                raise OSError("剪贴板被占用")
            opened = True
            if not self.user32.EmptyClipboard() or not self.user32.SetClipboardData(13, handle):
                raise OSError("无法写入剪贴板")
            handle = None  # Ownership transfers to Windows on success.
        finally:
            if opened:
                self.user32.CloseClipboard()
            if handle:
                kernel32.GlobalFree(handle)

    def _command(self, action, stop):
        timeout = action.get("timeoutMs", 60_000) / 1000
        deadline = time.monotonic() + timeout
        with self._job_lock:
            self._check(stop)
            self._job = ProcessJob([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", action["command"]],
                                   cwd=action.get("cwd") or None, env=os.environ.copy(),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            job = self._job
        output = [bytearray(), bytearray()]
        def collect(stream, target):
            while chunk := stream.read(8192):
                target.extend(chunk[:max(0, 32_769 - len(target))])
        threads = [threading.Thread(target=collect, args=(stream, output[index]), daemon=True)
                   for index, stream in enumerate((job.proc.stdout, job.proc.stderr))]
        for thread in threads:
            thread.start()
        try:
            while job.proc.poll() is None:
                if stop.wait(.05):
                    if not job.terminate(5):
                        raise OSError("未能确认命令进程已停止")
                    raise BatchStopped()
                if time.monotonic() >= deadline:
                    if not job.terminate(5):
                        raise OSError("命令超时且未能确认进程已停止")
                    raise TimeoutError("命令执行超时")
            # Close the job before joining readers: descendants may inherit the pipes.
            with self._job_lock:
                self._job = None
                job.close()
            for thread in threads:
                thread.join(5)
                if thread.is_alive():
                    raise OSError("无法结束命令输出读取")
            self._check(stop)
            return {"exitCode": job.proc.returncode, "stdout": decode_command_output(output[0]), "stderr": decode_command_output(output[1])}
        finally:
            with self._job_lock:
                self._job = None
                job.close()
            for thread, stream in zip(threads, (job.proc.stdout, job.proc.stderr)):
                thread.join(1)
                if not thread.is_alive() and stream:
                    stream.close()

    def execute(self, action, stop):
        previous = self.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        try:
            return self._execute(action, stop)
        finally:
            if previous:
                self.user32.SetThreadDpiAwarenessContext(previous)

    def _execute(self, action, stop):
        self._check(stop)
        kind = action["action"]
        if kind in ("move", "click", "drag", "scroll"):
            action = self._image_coordinates(action)
            self._move(action["x"], action["y"], stop)
        if kind == "click":
            button = action.get("button", "left")
            for _ in range(action.get("count", 1)):
                self._check(stop)
                self._button(button, True)
                self._button(button, False)
        elif kind == "drag":
            button = action.get("button", "left")
            self._button(button, True)
            self._move(action["endX"], action["endY"], stop)
            self._button(button, False)
        elif kind == "scroll":
            self._mouse(0x0800, action["amount"])
        elif kind == "key":
            self._keys(action["keys"], action.get("state", "press"), stop)
        elif kind == "wait":
            if stop.wait(action["milliseconds"] / 1000):
                raise BatchStopped()
        elif kind == "screenshot":
            image, info = capture_desktop(include_data=False)
            if action.get("scope") == "window":
                x, y, width, height, actual_scope = self._active_region(info)
                image = crop_image(image, [x-info["left"], y-info["top"], x-info["left"]+width, y-info["top"]+height])
                info.update(left=x, top=y, width=width, height=height, scope=actual_scope)
            monitor = None
            if "monitor" in action:
                monitor = next((m for m in info["monitors"] if m["id"] == action["monitor"]), None)
                if monitor is None:
                    raise ValueError("显示器编号不存在")
            if "region" in action:
                x, y, width, height = action["region"]
                if monitor:
                    if x < 0 or y < 0 or x + width > monitor["width"] or y + height > monitor["height"]:
                        raise ValueError("截图区域超出显示器范围")
                    x += monitor["left"]
                    y += monitor["top"]
                box = [x - info["left"], y - info["top"], x - info["left"] + width, y - info["top"] + height]
                image = crop_image(image, box)
                info.update(left=x, top=y, width=width, height=height)
            elif monitor:
                x, y, width, height = (monitor[k] for k in ("left", "top", "width", "height"))
                image = crop_image(image, [x-info["left"], y-info["top"], x-info["left"]+width, y-info["top"]+height])
                info.update(left=x, top=y, width=width, height=height)
            if "region" not in action and (action.get("scope") != "window" or info.get("scope") == "monitor"):
                image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
            info["imageWidth"], info["imageHeight"] = image.size
            if action.get("grid", False):
                draw = ImageDraw.Draw(image)
                for x in range(100, image.width, 100):
                    draw.line((x, 0, x, image.height - 1), fill=(255, 0, 255), width=1)
                for y in range(100, image.height, 100):
                    draw.line((0, y, image.width - 1, y), fill=(255, 0, 255), width=1)
                info["grid"] = {"spacing": 100, "origin": [0, 0], "width": 1, "color": "#ff00ff"}
            grayscale = action.get("grayscale", False)
            if grayscale and "grid" in info:
                info["grid"]["color"] = "#696969"
            output = io.BytesIO()
            image.convert("L" if grayscale else "RGB").save(output, format="JPEG", quality=85, subsampling=0)
            info.pop("image", None)
            info["imageId"] = uuid.uuid4().hex
            self.last_image = {key: info[key] for key in ("imageId", "left", "top", "width", "height", "imageWidth", "imageHeight")}
            return {**info, "mimeType": "image/jpeg", "grayscale": grayscale, "jpeg": output.getvalue()}
        elif kind == "clipboard_write":
            self._clipboard_write(action["text"])
        elif kind == "command":
            return self._command(action, stop)
        return {"ok": True}

    def release_all(self):
        errors = []
        for button in list(reversed(self.pressed_buttons)):
            try:
                self._button(button, False)
            except OSError as error:
                errors.append(str(error))
        for code in list(reversed(self.pressed_keys)):
            try:
                self._key(code, False)
                self.pressed_keys.remove(code)
            except OSError as error:
                errors.append(str(error))
        if errors:
            raise OSError("无法释放部分输入：" + "; ".join(errors))
