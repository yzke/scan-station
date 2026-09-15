"""Force-ending a batch that never reported a terminal status.

A batch that cannot finish keeps the physical device reserved, which is what
locks the station. Forcing it to end must release that reservation *and* make
sure the queued request on the scanner computer cannot scan by surprise later.
"""

import pytest

from documents import DocumentStore
from server import ScanCoordinator


class FakeEntry:
    def __init__(self, name):
        self._name = name

    def get_longname(self):
        return self._name


class FakeRequestShare:
    """Minimal stand-in for the scanner host's ``C$`` request directory.

    Entries are keyed by bare filename because a wildcard ``listPath`` reports
    long names without their directory, which is what ``smb_list`` callers in
    ``server.py`` compare against.
    """

    def __init__(self, names=()):
        self.files = {name: b"{}" for name in names}
        self.deleted = []
        self.closed = False

    def listPath(self, share, pattern):
        assert share == "C$"
        assert pattern == REQUEST_DIR + "/*", pattern
        return [FakeEntry(name) for name in sorted(self.files)]

    def deleteFile(self, share, path):
        assert share == "C$"
        prefix = REQUEST_DIR + "/"
        assert path.startswith(prefix), path
        name = path[len(prefix):]
        if name not in self.files:
            raise FileNotFoundError(name)
        del self.files[name]
        self.deleted.append(name)

    def close(self):
        self.closed = True


REQUEST_DIR = "Users/Public/scan-agent/req"


def make_store(tmp_path):
    # A passthrough processor keeps the test on metadata, not image processing.
    return DocumentStore(tmp_path / "documents", processor=lambda data: (data, {}))


def start_pending_batch(store, name):
    document = store.create(name)
    batch = store.start_batch(document["id"])
    return document, batch


def test_abandon_releases_the_reservation_and_withdraws_the_request(tmp_path):
    store = make_store(tmp_path)
    document, batch = start_pending_batch(store, "强制终止样例")
    share = FakeRequestShare({f"{batch['id']}.json"})
    coordinator = ScanCoordinator(store, connection_factory=lambda: share,
                                  worker_launcher=lambda *args: None)

    result = coordinator.abandon(document["id"])

    assert result["withdrawn"] is True
    assert result["already_finished"] is False
    assert result["document"]["state"] == "cancelled"
    # The reservation is gone, so the station accepts new work again.
    assert store.pending_batches() == []
    assert coordinator.active_id is None
    assert store.start_batch(document["id"])["pending"] is True
    # The queued request no longer exists on the scanner computer.
    assert share.deleted == [f"{batch['id']}.json"]
    assert share.files == {}
    assert share.closed is True
    # Nothing is left needing a retry.
    assert store.unwithdrawn_batches() == []
    assert "已撤回" in result["document"]["msg"]


def test_abandon_removes_an_interrupted_publish_left_as_upload(tmp_path):
    store = make_store(tmp_path)
    document, batch = start_pending_batch(store, "中断发布样例")
    share = FakeRequestShare({f"{batch['id']}.upload"})
    coordinator = ScanCoordinator(store, connection_factory=lambda: share,
                                  worker_launcher=lambda *args: None)

    result = coordinator.abandon(document["id"])

    assert result["withdrawn"] is True
    assert share.files == {}


def test_abandon_still_releases_when_the_host_is_unreachable(tmp_path):
    store = make_store(tmp_path)
    document, batch = start_pending_batch(store, "离线终止样例")

    def unreachable():
        raise OSError("scanner host is down")

    coordinator = ScanCoordinator(store, connection_factory=unreachable,
                                  worker_launcher=lambda *args: None)

    result = coordinator.abandon(document["id"])

    # The operator must not stay locked out just because the host is off.
    assert result["withdrawn"] is False
    assert store.pending_batches() == []
    assert coordinator.active_id is None
    # ...but the request still needs withdrawing once the host returns.
    assert store.unwithdrawn_batches() == [(document["id"], batch["id"])]
    assert "自动重试撤回" in result["document"]["msg"]


def test_sweep_withdraws_the_request_after_the_host_returns(tmp_path):
    store = make_store(tmp_path)
    document, batch = start_pending_batch(store, "重试撤回样例")
    share = FakeRequestShare({f"{batch['id']}.json"})
    reachable = {"value": False}

    def factory():
        if not reachable["value"]:
            raise OSError("scanner host is down")
        return share

    coordinator = ScanCoordinator(store, connection_factory=factory,
                                  worker_launcher=lambda *args: None)

    assert coordinator.abandon(document["id"])["withdrawn"] is False
    assert coordinator.sweep_withdrawals() == []
    assert store.unwithdrawn_batches() != []

    reachable["value"] = True
    assert coordinator.sweep_withdrawals() == [batch["id"]]
    assert store.unwithdrawn_batches() == []
    assert share.deleted == [f"{batch['id']}.json"]
    # A confirmed withdrawal is recorded, so it is never retried forever.
    assert coordinator.sweep_withdrawals() == []


def test_abandon_reports_a_batch_that_finished_first(tmp_path):
    store = make_store(tmp_path)
    document, batch = start_pending_batch(store, "先完成样例")
    share = FakeRequestShare({f"{batch['id']}.json"})

    def factory():
        # The agent reports a real result while the withdrawal is in flight.
        store.finish_batch(document["id"], batch["id"])
        return share

    coordinator = ScanCoordinator(store, connection_factory=factory,
                                  worker_launcher=lambda *args: None)

    result = coordinator.abandon(document["id"])

    assert result["already_finished"] is True
    # The scanner's own result wins over the operator's cancel.
    assert result["document"]["state"] == "done"


def test_abandon_refuses_when_nothing_is_pending(tmp_path):
    store = make_store(tmp_path)
    document = store.create("无任务样例")
    coordinator = ScanCoordinator(store, connection_factory=lambda: FakeRequestShare(),
                                  worker_launcher=lambda *args: None)

    with pytest.raises(ValueError):
        coordinator.abandon(document["id"])


def test_collect_stops_without_touching_the_host_once_abandoned(tmp_path):
    store = make_store(tmp_path)
    document, batch = start_pending_batch(store, "停止收集样例")
    share = FakeRequestShare({f"{batch['id']}.json"})
    coordinator = ScanCoordinator(store, connection_factory=lambda: share,
                                  worker_launcher=lambda *args: None)
    coordinator.abandon(document["id"])

    opened = []

    def factory():
        opened.append(1)
        return share

    coordinator.connection_factory = factory
    coordinator.collect(document["id"], batch["id"])

    # The abandoned worker must exit instead of polling the host forever.
    assert opened == []
