"""配置、原子凭据写入与跨进程实例锁。"""

import errno
import json
import os
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

DEFAULTS = {
    "host": "0.0.0.0", "port": 11452, "poll_seconds": 300,
    "jitter_seconds": 10, "language": "schinese", "data_dir": "data",
    "missing_confirmations": 2, "member_aliases": {},
}


@dataclass(frozen=True)
class Config:
    host: str = "0.0.0.0"
    port: int = 11452
    poll_seconds: int = 300
    jitter_seconds: int = 10
    language: str = "schinese"
    data_dir: Path = Path("data")
    missing_confirmations: int = 2
    member_aliases: dict[str, str] = field(default_factory=dict)
    api_secret: str = ""


def read_env(file: Path) -> dict[str, str]:
    """读取本项目使用的 KEY=value；环境变量优先于 .env。"""
    result = {}
    if file.exists():
        for line in file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, sep, value = line.partition("=")
            if sep:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                else:
                    value = value.split(" #", 1)[0].rstrip()
                result[key.strip()] = value
    return result


def load_config(root: str | Path | None = None) -> Config:
    root = Path(root or Path.cwd()).resolve()
    defaults_file = root / "config.example.json"
    values = dict(DEFAULTS)
    for file in (defaults_file, root / "config.json"):
        if file.exists():
            data = json.loads(file.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError(f"{file.name} 必须为 JSON 对象")
            values.update(data)
    for key, minimum, maximum in (
        ("port", 1, 65535), ("poll_seconds", 60, 86400),
        ("jitter_seconds", 0, 300), ("missing_confirmations", 1, 10),
    ):
        if type(values[key]) is not int or not minimum <= values[key] <= maximum:
            raise ValueError(f"config.json 中 {key} 必须为 {minimum}～{maximum} 的整数")
    if (any(not isinstance(values[key], str) for key in ("host", "data_dir", "language"))
            or not isinstance(values["member_aliases"], dict)):
        raise ValueError("config.json 的地址、路径、语言或成员别名无效")
    secret = os.environ.get("MONITOR_API_SECRET") or read_env(root / ".env").get("MONITOR_API_SECRET", "")
    if len(secret) < 32 or secret.startswith("replace-"):
        raise ValueError("请先运行 python -m steam_family_watchdog_core setup，或配置至少 32 字符的 MONITOR_API_SECRET")
    data_dir = (root / values["data_dir"]).resolve()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Config(**{key: values[key] for key in DEFAULTS if key != "data_dir"}, data_dir=data_dir, api_secret=secret)


def atomic_json(file: str | Path, data: object) -> None:
    file = Path(file)
    fd, temp = tempfile.mkstemp(prefix=f"{file.name}.", suffix=".tmp", dir=file.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, file)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _pid_exists(pid: int) -> bool:
    # os.kill(pid, 0) can terminate processes on Windows: use OpenProcess there.
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if handle:
            try:
                exit_code = wintypes.DWORD()
                if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    raise OSError(ctypes.get_last_error(), "无法确认实例锁，拒绝重复启动")
                return exit_code.value == 259  # STILL_ACTIVE; exited handles may linger.
            finally:
                kernel.CloseHandle(handle)
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: PID absent
            return False
        raise OSError(error, "无法确认实例锁，拒绝重复启动")
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError as error:
        raise RuntimeError("无法确认实例锁，拒绝重复启动") from error


def acquire_lock(data_dir: str | Path) -> Callable[[], None]:
    path = Path(data_dir) / "monitor.lock"
    while True:
        try:
            with path.open("x", encoding="ascii") as stream:
                if os.name != "nt":
                    os.chmod(path, 0o600)
                stream.write(str(os.getpid()))
            break
        except FileExistsError:
            original = path.read_text(encoding="ascii")
            try:
                pid = int(original)
                if pid <= 0:
                    raise ValueError
            except ValueError as error:
                raise RuntimeError("实例锁无效，请检查 data/monitor.lock") from error
            if _pid_exists(pid):
                raise RuntimeError("此数据目录已有进程运行，请先停止它")
            # Do not unlink a replacement installed by another starter.
            if path.read_text(encoding="ascii") == original:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
    def release() -> None:
        try:
            if path.read_text(encoding="ascii") == str(os.getpid()):
                path.unlink()
        except FileNotFoundError:
            pass
    return release


def setup(root: str | Path | None = None) -> None:
    root = Path(root or Path.cwd()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    config = root / "config.json"
    if not config.exists():
        atomic_json(config, DEFAULTS)
    try:
        fd = os.open(root / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as error:
        if error.errno != errno.EEXIST:
            raise
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(f"MONITOR_API_SECRET={secrets.token_hex(32)}\n")
