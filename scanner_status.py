"""Cached scanner heartbeat; HTTP callers never wait for Windows or SMB.

The Windows agent writes ``scanner-status.json`` beside (not inside) its request
directory after read-only WIA discovery. A missing or old heartbeat is unknown,
not proof that the physical scanner is powered off.

Reading that file is itself the liveness check for the scanner computer: a
successful read proves the computer answered, while the heartbeat timestamp
proves its agent is still running. Keeping the two apart lets the station tell
"computer is off" from "computer is up but nobody is listening", which matters
because a scan request reserves the device until an agent reports a terminal
status.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import threading
import time


SUPPORTED_DPI = (150, 200, 300)
_MESSAGES = {
    "online": "扫描仪已连接",
    "offline": "未检测到扫描仪，请检查电源和 USB 连接",
    "scanning": "扫描仪正在扫描",
    "unknown": "暂时无法确认扫描仪状态",
}


def _unknown(message=None, checked_at=None):
    return {
        "state": "unknown",
        "message": message or _MESSAGES["unknown"],
        "checked_at": checked_at,
        "supported_dpi": [],
        "supports_page_rescan": False,
    }


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Heartbeat has no UTC timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Heartbeat timestamp must specify its timezone")
    return parsed.timestamp()


def _normalize(payload):
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8-sig")
    if isinstance(payload, str):
        if len(payload) > 65536:
            raise ValueError("Heartbeat is too large")
        payload = json.loads(payload.lstrip("\ufeff"))
    if not isinstance(payload, dict):
        raise ValueError("Heartbeat must be an object")
    state = payload.get("state")
    if state not in _MESSAGES:
        raise ValueError("Unknown scanner state")
    checked_at = payload.get("checked_at")
    checked_timestamp = _timestamp(checked_at)
    result = {"state": state, "message": _MESSAGES[state],
              "checked_at": checked_at, "supported_dpi": [], "supports_page_rescan": False}
    version = payload.get("agent_version")
    if type(version) is int and version > 0:
        result["agent_version"] = version
    name = payload.get("name")
    if isinstance(name, str) and name.strip():
        result["name"] = name.strip()[:200]
    if state in ("online", "scanning"):
        result["supports_page_rescan"] = (type(version) is int and version >= 4
                                          and payload.get("supports_page_rescan") is True)
        values = payload.get("supported_dpi")
        if isinstance(values, list):
            result["supported_dpi"] = sorted({value for value in values
                if type(value) is int and value in SUPPORTED_DPI})
        if type(payload.get("duplex_supported")) is bool:
            result["duplex_supported"] = payload["duplex_supported"]
        mode = payload.get("capture_mode")
        if type(version) is int and version >= 5 and mode in (
                "wia2-preferred", "wia2-batch", "wia-automation-compat"):
            result["capture_mode"] = mode
            detail = payload.get("capture_mode_detail")
            if isinstance(detail, str):
                result["capture_mode_detail"] = "".join(c for c in detail if ord(c) >= 32)[:200]
    return result, checked_timestamp


class ScannerMonitor:
    """Poll an injected status reader in one daemon thread.

    ``read_status()`` returns JSON bytes/text or an already decoded dictionary.
    The reader must use a bounded SMB/socket timeout. ``snapshot()`` only takes
    a short memory lock, including when that reader is slow or unavailable.
    """

    def __init__(self, read_status, *, poll_seconds=5, stale_seconds=45,
                 clock=time.time):
        if poll_seconds <= 0 or stale_seconds <= 0:
            raise ValueError("Polling and freshness intervals must be positive")
        self._read_status = read_status
        self._poll_seconds = poll_seconds
        self._stale_seconds = stale_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._status = _unknown("正在等待扫描仪状态")
        self._checked_timestamp = None
        # Wall-clock time of the last read that actually reached the computer.
        # None means the scanner computer has not answered yet.
        self._contacted_at = None

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop.clear()
            self._thread = threading.Thread(target=self._run,
                                            name="scanner-monitor", daemon=True)
            self._thread.start()
        return self

    def close(self):
        self._stop.set()
        with self._lock:
            worker = self._thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=0.2)

    def _run(self):
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self._poll_seconds)

    def refresh(self):
        """Read once; used by the worker and deterministic, mocked tests."""
        try:
            status, checked_timestamp = _normalize(self._read_status())
        except Exception:
            status = _unknown("无法读取扫描仪状态，请检查 Windows 连接")
            checked_timestamp = None
            contacted_at = None
        else:
            contacted_at = self._clock()
        with self._lock:
            self._status = status
            self._checked_timestamp = checked_timestamp
            self._contacted_at = contacted_at
        return self.snapshot()

    def snapshot(self):
        with self._lock:
            result = deepcopy(self._status)
            checked_timestamp = self._checked_timestamp
            contacted_at = self._contacted_at
        fresh = False
        if checked_timestamp is not None:
            age = self._clock() - checked_timestamp
            fresh = -self._stale_seconds <= age <= self._stale_seconds
            if not fresh:
                result = _unknown("扫描仪状态已过期，正在等待设备心跳",
                                  result["checked_at"])
        # A successful read proves the computer answered; the heartbeat's own
        # timestamp proves its agent is still alive. Reported separately so a
        # caller can say which of the two is missing.
        result["host_reachable"] = contacted_at is not None
        result["heartbeat_fresh"] = fresh
        result["contacted_at"] = _iso(contacted_at)
        return result


def _iso(timestamp):
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def readiness_problem(snapshot):
    """Why a scan must not start yet, or None when the device is ready.

    A scan request reserves the physical device until the agent reports a
    terminal status, so starting one when nothing can consume it locks the
    station: the page never arrives and the reservation is never released.
    Requiring both a reachable computer and a live agent keeps that from
    happening instead of relying on the operator to notice.
    """
    if not snapshot.get("host_reachable"):
        return "无法连接扫描电脑（未开机或网络不通），请开机并确认扫描代理运行后再扫描。"
    if not snapshot.get("heartbeat_fresh"):
        return "扫描电脑已连接，但扫描代理没有运行，请在该电脑上启动 run-agent.cmd 后再扫描。"
    if snapshot.get("state") not in ("online", "scanning"):
        return snapshot.get("message") or "扫描仪当前不可用，请检查电源与 USB 连接后刷新状态。"
    return None
