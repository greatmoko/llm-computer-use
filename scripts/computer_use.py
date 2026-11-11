"""Local CLI for the model-independent computer-use skill (Windows)."""
import argparse
import asyncio
import json
import os
import queue
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from computer_use_session import DesktopSession
from computer_use_batch import MAX_ACTIONS, MAX_TEXT

if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(os.environ.get("LLM_COMPUTER_USE_STATE_DIR") or (Path(tempfile.gettempdir()) / "llm-computer-use"))
STATE = ROOT / "session.json"
LAST = ROOT / "last.json"
# 每个字符在 JSON 中最多占 6 字节（例如控制字符被转义），另留协议字段空间。
MAX_REQUEST = MAX_ACTIONS * MAX_TEXT * 6 + 100_000


def _publish_image(result, captures):
    if isinstance(result, dict):
        if isinstance(result.get("jpeg"), bytes):
            path = ROOT / ("capture-" + uuid.uuid4().hex + ".jpg")
            path.write_bytes(result.pop("jpeg"))
            captures.append(path)
            result["imagePath"] = str(path)
        for value in result.values():
            _publish_image(value, captures)
    elif isinstance(result, list):
        for item in result:
            _publish_image(item, captures)
    return result


def _read_state():
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
        if type(state.get("port")) is not int or not 1 <= state["port"] <= 65535 or not isinstance(state.get("token"), str):
            raise ValueError
        return state
    except (OSError, ValueError, AttributeError):
        raise RuntimeError("没有可用的电脑操作会话") from None


async def _request(state, command, actions=None):
    payload = {"token": state["token"], "command": command}
    if actions is not None:
        payload["actions"] = actions
    encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_REQUEST:
        raise ValueError("请求大小无效")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", state["port"], limit=MAX_REQUEST + 1)
    except OSError as error:
        try:
            last = json.loads(LAST.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            last = None
        detail = f"；最后状态：{json.dumps(last, ensure_ascii=False)}" if isinstance(last, dict) else "；服务可能意外退出，原因未知"
        raise RuntimeError(f"电脑操作服务无法连接（{error}）{detail}") from error
    try:
        writer.write(encoded)
        await writer.drain()
        raw = await reader.readline()
        if not raw:
            raise RuntimeError("电脑操作服务已断开")
        response = json.loads(raw)
        if response.get("error"):
            raise RuntimeError(response["error"])
        return response
    finally:
        writer.close()
        await writer.wait_closed()


async def _serve(initial_grid=False, initial_grayscale=False):
    session = DesktopSession()
    token = secrets.token_urlsafe(32)
    stopped = asyncio.Event()
    last_activity = time.monotonic()
    server = None
    state_written = False
    captures = []
    result = None
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
        initial = _publish_image(await session.start(grid=initial_grid, grayscale=initial_grayscale), captures)
        async def handle(reader, writer):
            nonlocal last_activity
            try:
                try:
                    raw = await asyncio.wait_for(reader.readline(), 5)
                except ValueError:
                    raise ValueError("请求大小无效") from None
                if not raw or len(raw) > MAX_REQUEST:
                    raise ValueError("请求大小无效")
                data = json.loads(raw)
                if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not secrets.compare_digest(data["token"], token):
                    raise ValueError("请求无效")
                command = data.get("command")
                last_activity = time.monotonic()
                if command == "actions":
                    response = _publish_image(await session.actions(data.get("actions")), captures)
                elif command == "status":
                    response = {"status": "stopping" if session.stopping else "running", "stepsCompleted": session.steps}
                elif command in ("stop", "finish"):
                    response = await session.close("agent" if command == "stop" else "finished")
                    stopped.set()
                else:
                    raise ValueError("请求无效")
            except Exception as error:
                response = {"error": str(error)[:1000]}
            try:
                writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(handle, "127.0.0.1", 0, limit=MAX_REQUEST + 1)
        state = {"port": server.sockets[0].getsockname()[1], "token": token, "pid": os.getpid()}
        tmp = ROOT / ("session-" + uuid.uuid4().hex + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, STATE)
        state_written = True
        LAST.unlink(missing_ok=True)
        print(json.dumps({"status": "running", "observation": initial}, ensure_ascii=False), flush=True)
        started = time.monotonic()
        while not stopped.is_set() and not session.stopping:
            if time.monotonic() - started >= 7200 or (not session.inflight and time.monotonic() - last_activity >= 1800):
                await session.close("timeout")
                break
            try:
                await asyncio.wait_for(stopped.wait(), .25)
            except asyncio.TimeoutError:
                pass
    except Exception as error:
        if not state_written:
            print(json.dumps({"error": str(error)[:1000]}, ensure_ascii=False), flush=True)
        else:
            session.error = f"电脑操作服务异常：{error}"[:1000]
        raise
    finally:
        if server:
            server.close()
            await server.wait_closed()
        if session.started:
            result = await session.close(session.reason or "finished")
        for capture in captures:
            capture.unlink(missing_ok=True)
        if state_written:
            if result is not None:
                LAST.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            try:
                if _read_state().get("token") == token:
                    STATE.unlink(missing_ok=True)
            except RuntimeError:
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("start", "actions", "status", "stop", "finish", "_serve"))
    parser.add_argument("--grid", action="store_true", help="在启动截图上添加参考线")
    parser.add_argument("--grayscale", action="store_true", help="启动截图使用灰度")
    args = parser.parse_args()
    if (args.grid or args.grayscale) and args.command not in ("start", "_serve"):
        parser.error("--grid 和 --grayscale 仅用于 start")
    if args.command == "_serve":
        asyncio.run(_serve(initial_grid=args.grid, initial_grayscale=args.grayscale))
        return
    if args.command == "start":
        ROOT.mkdir(parents=True, exist_ok=True)
        # The daemon owns the OS desktop lock; an existing live session is never replaced.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        child = subprocess.Popen([sys.executable, "-u", __file__, "_serve"] + (["--grid"] if args.grid else []) + (["--grayscale"] if args.grayscale else []), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=flags)
        output = queue.Queue()
        threading.Thread(target=lambda: output.put(child.stdout.readline()), daemon=True).start()
        try:
            raw = output.get(timeout=20)
        except queue.Empty:
            child.kill()
            child.wait()
            raise RuntimeError("电脑操作启动超时") from None
        finally:
            child.stdout.close()
        result = json.loads(raw) if raw else {"error": "电脑操作进程未启动"}
        if result.get("error"):
            raise RuntimeError(result["error"])
    else:
        actions = json.load(sys.stdin) if args.command == "actions" else None
        try:
            state = _read_state()
        except RuntimeError:
            if args.command != "status" or not LAST.is_file():
                raise
            result = json.loads(LAST.read_text(encoding="utf-8"))
        else:
            result = asyncio.run(_request(state, args.command, actions))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error": str(error)[:1000]}, ensure_ascii=False))
        raise SystemExit(1)
