"""Model-independent Windows desktop session for a calling agent.

The caller supplies observations to its own model and forwards its actions;
this module never starts a model or a Pi session.
"""
import asyncio
import ctypes
import sys
from ctypes import wintypes

from computer_use_batch import BatchRunner, validate_batch
from computer_use_guard_process import GuardProcess
from computer_use_windows import WindowsDesktopDriver


class DesktopLock:
    def __init__(self):
        if sys.platform != "win32":
            raise OSError("电脑操作目前仅支持 Windows")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel = kernel
        ctypes.set_last_error(0)
        self.handle = kernel.CreateMutexW(None, False, "Local\\BeeTeamComputerUseDesktop")
        if not self.handle:
            raise OSError("无法创建电脑操作锁")
        if ctypes.get_last_error() == 183:
            kernel.CloseHandle(self.handle)
            self.handle = None
            raise RuntimeError("电脑正在执行其他操作任务")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class DesktopSession:
    def __init__(self, move_duration_ms=100, *, driver_factory=WindowsDesktopDriver,
                 guard_factory=GuardProcess, lock_factory=DesktopLock):
        self.runner = BatchRunner(driver_factory(move_duration_ms / 1000), lambda action, stop: True)
        self.guard_factory = guard_factory
        self.lock_factory = lock_factory
        self.guard = None
        self.desktop_lock = None
        self.started = False
        self.stopping = False
        self.reason = None
        self.error = None
        self.cleanup_error = None
        self.steps = 0
        self.action_count = 0
        self.inflight = set()
        self.timer = None
        self.startup_done = asyncio.Event()
        self.close_lock = asyncio.Lock()

    def _guard_event(self, reason, detail=""):
        if reason == "guard_exited":
            self.error = "屏幕提示或 Esc 监听进程意外退出" + (f"：{detail}" if detail else "")
        self.reason = reason
        self.runner.stop()
        asyncio.create_task(self.close(reason))

    async def start(self, grid=False, grayscale=False):
        if self.started or self.stopping:
            raise RuntimeError("电脑操作会话已启动或停止")
        self.started = True
        try:
            self.desktop_lock = self.lock_factory()
            self.guard = self.guard_factory(self._guard_event)
            await self.guard.start()
            if self.stopping:
                raise RuntimeError("电脑操作已停止")
            initial = await asyncio.to_thread(self.runner.run, [
                {"action": "key", "keys": ["WIN", "D"]},
                {"action": "wait", "milliseconds": 500},
                {"action": "screenshot", "monitor": 1, "grid": grid, "grayscale": grayscale},
            ])
            if initial["status"] != "completed":
                raise RuntimeError(initial.get("error") or "无法显示桌面或截图")
            self.timer = asyncio.create_task(self._deadline())
            return initial["results"][-1]["result"]
        except BaseException:
            self.startup_done.set()
            await self.close("startup_failed")
            raise
        finally:
            self.startup_done.set()

    async def actions(self, actions):
        if not self.started or self.stopping or not self.startup_done.is_set():
            raise RuntimeError("电脑操作会话不可用")
        if isinstance(actions, dict):
            if (set(actions) - {"actions", "captureAfter", "captureAfterGrid", "captureAfterGrayscale"}
                    or type(actions.get("captureAfter", False)) is not bool
                    or type(actions.get("captureAfterGrid", False)) is not bool
                    or type(actions.get("captureAfterGrayscale", False)) is not bool
                    or ((actions.get("captureAfterGrid", False) or actions.get("captureAfterGrayscale", False)) and not actions.get("captureAfter", False))):
                raise ValueError("批次参数无效")
            capture_after = actions.get("captureAfter", False)
            capture_grid = actions.get("captureAfterGrid", False)
            capture_grayscale = actions.get("captureAfterGrayscale", False)
            actions = actions.get("actions")
            if not isinstance(actions, list):
                raise ValueError("操作数量无效")
            actions = list(actions)
            if capture_after and (not actions or not isinstance(actions[-1], dict) or actions[-1].get("action") != "screenshot"):
                actions.append({"action": "screenshot", "scope": "window", "grid": capture_grid, "grayscale": capture_grayscale})
            elif capture_after and (capture_grid or capture_grayscale):
                actions[-1] = {**actions[-1], **({"grid": True} if capture_grid else {}), **({"grayscale": True} if capture_grayscale else {})}
        actions = validate_batch(actions)
        if self.action_count + len(actions) > 1000:
            raise ValueError("电脑操作已达到步骤上限")
        self.action_count += len(actions)
        task = asyncio.create_task(asyncio.to_thread(self.runner.run, actions))
        self.inflight.add(task)
        try:
            result = await asyncio.shield(task)
            self.steps += len(result["results"])
            result["message"] = "操作批次已完成" if result["status"] == "completed" else "操作批次未完成"
            return result
        except Exception as error:
            self.error = f"电脑操作执行器异常：{error}"
            await self.close("executor_error")
            raise
        finally:
            if task.done():
                self.inflight.discard(task)

    async def close(self, reason="finished"):
        if not self.stopping:
            self.stopping = True
            self.reason = self.reason or reason
            self.runner.stop()
        await self.startup_done.wait()
        if self.timer and self.timer is not asyncio.current_task():
            self.timer.cancel()
            await asyncio.gather(self.timer, return_exceptions=True)
            self.timer = None
        async with self.close_lock:
            if self.cleanup_error:
                raise RuntimeError(self.cleanup_error)
            if self.inflight:
                results = await asyncio.gather(*self.inflight, return_exceptions=True)
                self.inflight.clear()
                for result in results:
                    if isinstance(result, BaseException):
                        self.error = self.error or f"电脑操作清理失败：{result}"
            if self.guard:
                try:
                    await self.guard.close()
                except Exception as error:
                    self.cleanup_error = f"无法确认屏幕提示已关闭：{error}"
                    self.error = self.error or self.cleanup_error
                    raise RuntimeError(self.cleanup_error) from error
                self.guard = None
            if self.desktop_lock:
                self.desktop_lock.close()
                self.desktop_lock = None
        return {"status": "failed" if self.error else "stopped" if self.reason != "finished" else "completed",
                "reason": self.reason, "error": self.error, "stepsCompleted": self.steps}

    async def _deadline(self):
        await asyncio.sleep(7200)
        await self.close("timeout")
