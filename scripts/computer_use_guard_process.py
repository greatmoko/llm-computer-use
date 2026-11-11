"""Supervise the desktop indicator and forward its emergency stop signal."""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

from winjob import ProcessJob


class GuardProcess:
    def __init__(self, on_stop):
        self.on_stop = on_stop
        self.job = None
        self.monitor = None
        self.stderr_task = None
        self.stderr_tail = bytearray()
        self.closing = False

    async def start(self):
        if self.job is not None:
            raise RuntimeError("屏幕提示已启动")
        if sys.platform != "win32":
            raise RuntimeError("当前运行环境不支持屏幕提示")
        path = Path(__file__).with_name("computer_use_guard.py")
        command = [sys.executable, "--computer-use-guard"] if getattr(sys, "frozen", False) else [sys.executable, "-u", str(path)]
        spawning = asyncio.create_task(asyncio.to_thread(
            ProcessJob, command, str(path.parent), os.environ.copy(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
        try:
            try:
                self.job = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                self.job = await spawning
                raise
            self.stderr_task = asyncio.create_task(self._read_stderr(self.job.proc.stderr))
            ready = await asyncio.wait_for(asyncio.to_thread(self.job.proc.stdout.readline), 10)
            if ready.strip() != b"READY":
                try:
                    await asyncio.wait_for(asyncio.shield(self.stderr_task), .25)
                except asyncio.TimeoutError:
                    pass
                detail = self.stderr_tail.decode("utf-8", errors="replace").strip()[-1000:]
                raise RuntimeError("无法启动屏幕提示或 Esc 紧急停止" + (f"：{detail}" if detail else ""))
            self.monitor = asyncio.create_task(self._monitor())
        except BaseException:
            await self.close()
            raise

    async def _read_stderr(self, stream):
        while chunk := await asyncio.to_thread(stream.read, 4096):
            self.stderr_tail.extend(chunk)
            del self.stderr_tail[:-4096]

    async def _monitor(self):
        proc = self.job.proc
        signalled = False
        try:
            while True:
                line = await asyncio.to_thread(proc.stdout.readline)
                if not line:
                    break
                if line.strip() == b"STOP":
                    signalled = True
                    self.on_stop("double_escape")
                    break
        finally:
            if not self.closing and not signalled:
                # An invisible task must never continue after losing its guard.
                if self.stderr_task:
                    try:
                        await asyncio.wait_for(asyncio.shield(self.stderr_task), .25)
                    except asyncio.TimeoutError:
                        pass
                detail = self.stderr_tail.decode("utf-8", errors="replace").strip()[-1000:]
                self.on_stop("guard_exited", detail)

    async def close(self):
        self.closing = True
        job, self.job = self.job, None
        if job is None:
            return
        if job.proc.stdin:
            job.proc.stdin.close()
        try:
            await asyncio.wait_for(asyncio.to_thread(job.proc.wait), 2)
        except asyncio.TimeoutError:
            if not await asyncio.to_thread(job.terminate, 5):
                raise RuntimeError("无法确认屏幕提示进程已退出")
        finally:
            await asyncio.to_thread(job.close)
            if self.monitor is not None:
                await self.monitor
                self.monitor = None
            if self.stderr_task is not None:
                await self.stderr_task
                self.stderr_task = None
            if job.proc.stdout:
                job.proc.stdout.close()
            if job.proc.stderr:
                job.proc.stderr.close()
