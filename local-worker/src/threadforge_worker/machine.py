"""Stable machine fingerprint for device identity (device_id 加固, §device_id).

每次重新配对都会生成随机 ``device_id``，导致同一台物理机在控制面留下多条
设备记录，且绑定在该 device 上的 Provider / 会话历史随旧 device 被撤销而
“悬空”。为根治，Worker 从上报一个**稳定机器指纹**（machine fingerprint）；
控制面在配对时按 “owner + 指纹” 去重并做“复用/接管”，而不是每次新建。

指纹来源优先级（跨重装保持稳定是关键）：
1. Windows：``HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid``
   （操作系统安装时生成，重装系统才变；重装 Worker 保持不变）。
2. Linux / macOS：``/etc/machine-id``（或 ``/var/lib/dbus/machine-id``）。
3. 回退：``uuid.getnode()``（网卡 MAC）+ hostname 的哈希——仅当上面都不存在
   时使用，此时指纹在克隆/换网卡场景可能变化，但已是尽力而为。

生成的值以 ``sha256`` 缩短为 32 hex，前缀 ``fp_``，与 device_id 同形态。
该指纹是 Worker 本机的稳定锚，**仅用于控制面去重/复用，不作为鉴权依据**。
"""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from pathlib import Path

# Windows MachineGuid 注册表路径。
_WIN_MACHINE_GUID_PATH = r"SOFTWARE\Microsoft\Cryptography"
_WIN_MACHINE_GUID_NAME = "MachineGuid"

# Linux / macOS machine-id 候选。
_MACHINE_ID_PATHS = (
    "/etc/machine-id",
    "/var/lib/dbus/machine-id",
)


def _read_windows_machine_guid() -> str | None:
    """Read Windows MachineGuid without a hard winreg dependency."""
    if sys.platform != "win32":
        return None
    try:
        import winreg  # type: ignore[name-defined]  # available on Windows

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, _WIN_MACHINE_GUID_PATH
        ) as key:
            value, _ = winreg.QueryValueEx(key, _WIN_MACHINE_GUID_NAME)
            return str(value).strip() or None
    except Exception:
        # 读注册表失败（权限/环境）不致命，交给后续回退。
        return None


def _read_machine_id_file() -> str | None:
    if sys.platform == "win32":
        return None
    for path in _MACHINE_ID_PATHS:
        try:
            text = Path(path).read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            continue
    return None


def _fallback_fingerprint() -> str:
    # 网卡 MAC（uuid.getnode）+ hostname：仅当系统级 machine-id 不可用时使用。
    try:
        node = uuid.getnode()
    except Exception:
        node = 0
    hostname = os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or ""
    return f"{node:x}-{hostname}"


def machine_fingerprint() -> str:
    """Return a stable ``fp_<32hex>`` fingerprint for this machine.

    The value is derived from OS-level machine identity so it survives Worker
    re-install / re-pair, letting the control plane reuse the same Device
    instead of creating duplicates (see §device_id hardening).
    """
    raw = (
        _read_windows_machine_guid()
        or _read_machine_id_file()
        or _fallback_fingerprint()
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"fp_{digest}"
