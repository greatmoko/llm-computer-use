"""Batch validation and cancellation for the computer-use executor.

This module deliberately does not expose a desktop control endpoint. A caller must
provide both an executor and an authorization callback before it can run a batch.
"""
import threading

MAX_ACTIONS = 32
MAX_WAIT_MS = 30_000
MAX_TEXT = 100_000
BUTTONS = {"left", "right", "middle"}
KEY_STATES = {"press", "down", "up"}


class BatchStopped(Exception):
    pass


def _integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} 无效")


def validate_batch(actions):
    """Validate every action before any side effects, returning a defensive copy."""
    if not isinstance(actions, list) or not 1 <= len(actions) <= MAX_ACTIONS:
        raise ValueError("操作数量无效")
    validated = []
    schemas = {
        "click": ({"action", "x", "y"}, {"button", "count", "imageId", "imageSize"}),
        "move": ({"action", "x", "y"}, {"imageId", "imageSize"}),
        "drag": ({"action", "x", "y", "endX", "endY"}, {"button", "imageId", "imageSize"}),
        "scroll": ({"action", "x", "y", "amount"}, {"imageId", "imageSize"}),
        "key": ({"action", "keys"}, {"state"}),
        "wait": ({"action", "milliseconds"}, set()),
        "screenshot": ({"action"}, {"region", "monitor", "scope", "grid", "grayscale"}),
        "clipboard_write": ({"action", "text"}, set()),
        "command": ({"action", "command"}, {"cwd", "timeoutMs"}),
    }
    for item in actions:
        if not isinstance(item, dict) or not isinstance(item.get("action"), str) or item["action"] not in schemas:
            raise ValueError("操作类型无效")
        required, optional = schemas[item["action"]]
        if not required <= item.keys() or item.keys() - required - optional:
            raise ValueError("操作参数无效")
        action = dict(item)
        for key in ("x", "y", "endX", "endY"):
            if key in action:
                _integer(action[key], key, -100_000, 100_000)
        if "imageId" in action and (not isinstance(action["imageId"], str) or not 1 <= len(action["imageId"]) <= 64):
            raise ValueError("截图标识无效")
        if "imageSize" in action:
            size = action["imageSize"]
            if "imageId" not in action or not isinstance(size, list) or len(size) != 2:
                raise ValueError("截图显示尺寸无效")
            for value in size:
                _integer(value, "截图显示尺寸", 1, 100_000)
            action["imageSize"] = list(size)
        if "scope" in action and (action["scope"] != "window" or "region" in action or "monitor" in action):
            raise ValueError("截图范围无效")
        if "grid" in action and type(action["grid"]) is not bool:
            raise ValueError("截图参考线参数无效")
        if "grayscale" in action and type(action["grayscale"]) is not bool:
            raise ValueError("灰度截图参数无效")
        if "button" in action and (not isinstance(action["button"], str) or action["button"] not in BUTTONS):
            raise ValueError("鼠标按键无效")
        if "count" in action:
            _integer(action["count"], "点击次数", 1, 2)
        if action["action"] == "scroll":
            _integer(action["amount"], "滚动量", -10_000, 10_000)
            if not action["amount"]:
                raise ValueError("滚动量无效")
        if action["action"] == "key":
            keys = action["keys"]
            if not isinstance(keys, list) or not 1 <= len(keys) <= 8 or any(not isinstance(k, str) or not k or len(k) > 32 for k in keys):
                raise ValueError("键盘按键无效")
            if not isinstance(action.get("state", "press"), str) or action.get("state", "press") not in KEY_STATES:
                raise ValueError("键盘状态无效")
            action["keys"] = list(keys)
        if action["action"] == "wait":
            _integer(action["milliseconds"], "等待时间", 0, MAX_WAIT_MS)
        if "region" in action:
            region = action["region"]
            if not isinstance(region, list) or len(region) != 4:
                raise ValueError("截图区域无效")
            for value in region:
                _integer(value, "截图区域", -100_000, 100_000)
            if region[2] <= 0 or region[3] <= 0:
                raise ValueError("截图区域无效")
            action["region"] = list(region)
        if "monitor" in action:
            _integer(action["monitor"], "显示器编号", 1, 256)
        if action["action"] == "clipboard_write" and (not isinstance(action["text"], str) or len(action["text"]) > MAX_TEXT or "\0" in action["text"]):
            raise ValueError("剪贴板内容无效")
        if action["action"] == "command":
            if not isinstance(action["command"], str) or not action["command"].strip() or len(action["command"]) > 10_000:
                raise ValueError("命令无效")
            if "cwd" in action and (not isinstance(action["cwd"], str) or not action["cwd"].strip() or len(action["cwd"]) > 4096):
                raise ValueError("工作目录无效")
            if "timeoutMs" in action:
                _integer(action["timeoutMs"], "命令超时", 1, 3_600_000)
        validated.append(action)
    return validated


class BatchRunner:
    """Run one validated batch; the driver must honor the shared stop event."""

    def __init__(self, driver, authorize):
        if not callable(authorize):
            raise ValueError("必须提供操作授权检查")
        self.driver = driver
        self.authorize = authorize
        self.stopped = threading.Event()
        self._lock = threading.Lock()

    def stop(self):
        self.stopped.set()
        self.driver.interrupt()

    def run(self, actions):
        actions = validate_batch(actions)
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("操作批次正在执行")
        results = []
        try:
            for index, action in enumerate(actions):
                if self.stopped.is_set():
                    return {"status": "stopped", "results": results, "nextIndex": index}
                try:
                    if not self.authorize(action, self.stopped):
                        return {"status": "stopped" if self.stopped.is_set() else "denied", "results": results, "nextIndex": index}
                    if self.stopped.is_set():
                        return {"status": "stopped", "results": results, "nextIndex": index}
                    value = self.driver.execute(action, self.stopped)
                    results.append({"index": index, "result": value})
                except BatchStopped:
                    return {"status": "stopped", "results": results, "nextIndex": index}
                except Exception as error:
                    return {"status": "failed", "results": results, "nextIndex": index, "error": str(error)}
            return {"status": "stopped" if self.stopped.is_set() else "completed", "results": results, "nextIndex": len(actions)}
        finally:
            try:
                self.driver.release_all()
            finally:
                self._lock.release()
