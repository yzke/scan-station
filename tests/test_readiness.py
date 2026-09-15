"""Starting a scan must be refused while nothing can consume the request.

A scan request reserves the physical device until the scanning agent reports a
terminal status. If the scanner computer is off, or its agent is not running,
that terminal status never arrives, so the reservation is held forever and the
station refuses all further work. The preflight gate exists to make that
impossible to trigger from the web page.
"""

import time

import pytest

from scanner_status import ScannerMonitor, readiness_problem


def heartbeat(state="online", checked_at=None, **extra):
    payload = {"state": state, "agent_version": 5, "supported_dpi": [150, 200, 300],
               "duplex_supported": True, "supports_page_rescan": True,
               "checked_at": checked_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    payload.update(extra)
    return payload


def monitor_reading(reader, **kwargs):
    return ScannerMonitor(reader, **kwargs)


def test_a_fresh_heartbeat_reports_both_signals():
    monitor = monitor_reading(lambda: heartbeat())
    snapshot = monitor.refresh()

    assert snapshot["host_reachable"] is True
    assert snapshot["heartbeat_fresh"] is True
    assert readiness_problem(snapshot) is None
    assert snapshot["contacted_at"].endswith("Z")


def test_an_unreadable_status_means_the_computer_did_not_answer():
    def unreachable():
        raise OSError("[Errno Connection error] timed out")

    snapshot = monitor_reading(unreachable).refresh()

    assert snapshot["host_reachable"] is False
    assert snapshot["heartbeat_fresh"] is False
    problem = readiness_problem(snapshot)
    assert problem is not None and "无法连接扫描电脑" in problem


def test_a_reachable_computer_with_a_stale_heartbeat_means_the_agent_is_not_running():
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
    monitor = monitor_reading(lambda: heartbeat(checked_at=old), stale_seconds=45)
    snapshot = monitor.refresh()

    # The read succeeded, so the computer is up...
    assert snapshot["host_reachable"] is True
    # ...but nothing is updating the heartbeat, so no agent is listening.
    assert snapshot["heartbeat_fresh"] is False
    problem = readiness_problem(snapshot)
    assert problem is not None and "扫描代理没有运行" in problem


def test_the_two_failures_are_distinguishable():
    def unreachable():
        raise OSError("down")

    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
    computer_off = readiness_problem(monitor_reading(unreachable).refresh())
    agent_down = readiness_problem(
        monitor_reading(lambda: heartbeat(checked_at=old), stale_seconds=45).refresh())

    assert computer_off != agent_down
    assert "未开机" in computer_off
    assert "run-agent.cmd" in agent_down


def test_a_fresh_heartbeat_without_a_scanner_is_refused():
    snapshot = monitor_reading(lambda: heartbeat(state="offline")).refresh()

    assert snapshot["host_reachable"] is True
    assert snapshot["heartbeat_fresh"] is True
    problem = readiness_problem(snapshot)
    assert problem is not None and "电源" in problem


def test_a_busy_scanner_still_allows_a_request():
    # "scanning" is online and working; the reservation check handles overlap.
    snapshot = monitor_reading(lambda: heartbeat(state="scanning")).refresh()
    assert readiness_problem(snapshot) is None


def test_an_unknown_snapshot_is_refused_rather_than_assumed_ready():
    # No monitor at all: the station cannot confirm anything, so it must refuse.
    problem = readiness_problem({})
    assert problem is not None and "无法连接扫描电脑" in problem


def test_a_failed_read_clears_a_previous_good_contact():
    payload = {"value": heartbeat()}

    def reader():
        value = payload["value"]
        if isinstance(value, Exception):
            raise value
        return value

    monitor = monitor_reading(reader)
    assert monitor.refresh()["host_reachable"] is True

    payload["value"] = OSError("computer went away")
    snapshot = monitor.refresh()

    assert snapshot["host_reachable"] is False
    assert snapshot["heartbeat_fresh"] is False
