from datetime import datetime, timezone
import json
import threading
import time

import pytest

from scanner_status import ScannerMonitor


NOW = 1_800_000_000


def heartbeat(state="online", age=0, **extra):
    return {"state": state, "checked_at": datetime.fromtimestamp(
        NOW - age, timezone.utc).isoformat(), **extra}


def monitor(value):
    return ScannerMonitor(lambda: value, clock=lambda: NOW)


def test_online_capabilities_are_driver_reported_and_filtered():
    value = heartbeat(name="A4 SFC Scanner", supported_dpi=[600, 300, 150, 300],
                      duplex_supported=True)
    result = monitor(json.dumps(value).encode()).refresh()
    assert result["state"] == "online"
    assert result["name"] == "A4 SFC Scanner"
    assert result["supported_dpi"] == [150, 300]
    assert result["duplex_supported"] is True


def test_missing_capability_is_unknown_not_unsupported():
    result = monitor(heartbeat()).refresh()
    assert result["supported_dpi"] == []
    assert "duplex_supported" not in result


def test_known_non_duplex_device_is_explicitly_false():
    assert monitor(heartbeat(duplex_supported=False)).refresh()[
        "duplex_supported"] is False


def test_missing_device_is_offline_but_reader_failure_is_unknown():
    result = monitor(heartbeat("offline", supported_dpi=[150],
                               duplex_supported=True)).refresh()
    assert result["state"] == "offline"
    assert result["supported_dpi"] == []
    assert "duplex_supported" not in result
    def failing_reader():
        raise OSError("SMB host unavailable")
    assert ScannerMonitor(failing_reader).refresh()["state"] == "unknown"


@pytest.mark.parametrize("age", [46, -46])
def test_old_or_future_heartbeat_does_not_report_connected(age):
    result = monitor(heartbeat(age=age, supported_dpi=[150],
                               duplex_supported=True)).refresh()
    assert result["state"] == "unknown"
    assert result["supported_dpi"] == []


def test_snapshot_expires_without_another_network_read():
    clock = [NOW]
    mon = ScannerMonitor(lambda: heartbeat(), clock=lambda: clock[0])
    assert mon.refresh()["state"] == "online"
    clock[0] += 46
    assert mon.snapshot()["state"] == "unknown"


@pytest.mark.parametrize("value", [None, b"partial{", [], {},
    {"state": "online", "checked_at": "not a timestamp"},
    {"state": "online", "checked_at": "2026-09-13T12:00:00"},
    {"state": "bad", "checked_at": "2026-09-13T12:00:00Z"}])
def test_invalid_or_partial_status_is_unknown(value):
    assert monitor(value).refresh()["state"] == "unknown"


def test_bom_and_scanning_status():
    value = "\ufeff" + json.dumps(heartbeat("scanning", supported_dpi=[200]))
    result = monitor(value.encode("utf-8")).refresh()
    assert result["state"] == "scanning"
    assert result["supported_dpi"] == [200]


def test_snapshot_does_not_wait_for_a_stuck_reader_or_mutate_the_cache():
    entered = threading.Event()
    release = threading.Event()
    def blocked_reader():
        entered.set()
        release.wait(2)
        return heartbeat(supported_dpi=[150])
    mon = ScannerMonitor(blocked_reader, clock=lambda: NOW).start()
    try:
        assert entered.wait(1)
        started = time.monotonic()
        assert mon.snapshot()["state"] == "unknown"
        assert time.monotonic() - started < 0.1
        release.set()
    finally:
        mon.close()
    mon.refresh()
    mon.snapshot()["supported_dpi"].append(300)
    assert mon.snapshot()["supported_dpi"] == [150]


def test_status_recovers_from_offline_and_network_failure():
    values = iter([heartbeat("offline"), None, heartbeat("online")])
    mon = ScannerMonitor(lambda: next(values), clock=lambda: NOW)
    assert [mon.refresh()["state"] for _ in range(3)] == [
        "offline", "unknown", "online"]
@pytest.mark.parametrize("state,version,capability,expected", [
    ("online", 4, True, True), ("scanning", 4, True, True),
    ("online", 3, True, False), ("online", "4", True, False),
    ("online", None, True, False), ("online", 4, None, False),
    ("online", 4, "true", False), ("online", 4, False, False),
    ("offline", 4, True, False), ("unknown", 4, True, False),
])
def test_rescan_capability_requires_fresh_v4_state(state, version, capability, expected):
    clock = [1_800_000_000.0]
    payload = {"state": state, "checked_at": datetime.fromtimestamp(clock[0], timezone.utc).isoformat(),
               "agent_version": version, "supports_page_rescan": capability}
    monitor = ScannerMonitor(lambda: payload, clock=lambda: clock[0])
    assert monitor.refresh()["supports_page_rescan"] is expected
    clock[0] += 60
    assert monitor.snapshot()["supports_page_rescan"] is False


@pytest.mark.parametrize("mode", ["wia2-preferred", "wia2-batch", "wia-automation-compat"])
def test_capture_mode_is_explicit_and_expires(mode):
    clock = [NOW]
    payload = heartbeat(agent_version=5, capture_mode=mode, capture_mode_detail="连续\n" + "字" * 250)
    mon = ScannerMonitor(lambda: payload, clock=lambda: clock[0])
    result = mon.refresh()
    assert result["capture_mode"] == mode
    assert "\n" not in result["capture_mode_detail"] and len(result["capture_mode_detail"]) == 200
    clock[0] += 46
    assert "capture_mode" not in mon.snapshot()
    assert "capture_mode_detail" not in mon.snapshot()


@pytest.mark.parametrize("state,version,mode", [("offline", 5, "wia2-batch"),
    ("unknown", 5, "wia2-batch"), ("online", 4, "wia2-batch"),
    ("online", "5", "wia2-batch"), ("online", 5, "invented")])
def test_unconfirmed_capture_modes_are_hidden(state, version, mode):
    assert "capture_mode" not in monitor(heartbeat(state, agent_version=version, capture_mode=mode)).refresh()
