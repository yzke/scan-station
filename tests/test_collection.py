import io
import json

import pytest
from PIL import Image

from documents import DocumentStore
from server import OUT_DIR, REQ_DIR, ScanBusy, ScanCoordinator, terminal_result


def jpeg(color="white"):
    output = io.BytesIO()
    Image.new("RGB", (60, 90), color).save(output, "JPEG")
    return output.getvalue()


class Entry:
    def __init__(self, name):
        self.name = name

    def get_longname(self):
        return self.name


class FakeSMB:
    """Memory-only transport; request writes never touch SMB or the filesystem."""
    def __init__(self, phases, before_list=None):
        self.phases = phases
        self.before_list = before_list
        self.index = -1
        self.files = {}
        self.writes = []
        self.uploads = {}
        self.closed = False

    def listPath(self, share, path):
        assert share == "C$" and path == OUT_DIR + "/*"
        self.index += 1
        if self.before_list:
            self.before_list(self.index)
        phase = self.phases[min(self.index, len(self.phases) - 1)]
        if isinstance(phase, Exception):
            raise phase
        self.files = phase
        return [Entry(name) for name in self.files]

    def getFile(self, share, path, callback):
        value = self.files[path.rsplit("/", 1)[1]]
        if isinstance(value, Exception):
            raise value
        callback(value)

    def putFile(self, share, path, callback):
        assert path.startswith(REQ_DIR + "/") and path.endswith(".upload")
        chunks = []
        while chunk := callback(1024):
            chunks.append(chunk)
        self.uploads[path] = b"".join(chunks)

    def rename(self, share, source, destination):
        assert source.endswith(".upload") and destination.endswith(".json")
        self.writes.append((destination, json.loads(self.uploads.pop(source))))

    def close(self):
        self.closed = True


class FakeStop:
    def __init__(self, limit=30):
        self.waits = 0
        self.limit = limit
        self.stopped = False

    def is_set(self):
        return self.stopped or self.waits >= self.limit

    def wait(self, seconds):
        self.waits += 1

    def set(self):
        self.stopped = True


def setup_batch(tmp_path, *, dpi=150, duplex=False):
    store = DocumentStore(tmp_path, processor=lambda data: (data, {}))
    sid = store.create("测试")["id"]
    store.start_batch(sid, "batch", dpi=dpi, duplex=duplex)
    return store, sid


def coordinator(store, fake, *, limit=30, timeout=300):
    result = ScanCoordinator(store, connection_factory=lambda: fake, poll_seconds=0,
                             timeout=timeout, worker_launcher=lambda *args: None)
    result.stop = FakeStop(limit)
    result.clock = lambda: result.stop.waits * 10
    return result


def test_pages_publish_before_status_and_partial_pages_retry(tmp_path):
    store, sid = setup_batch(tmp_path, dpi=300, duplex=True)
    full = jpeg()
    observations = []

    def check(index):
        document = store.get(sid)
        observations.append((index, document["pages"], document["state"]))

    fake = FakeSMB([
        {"batch-p1.jpg": full[:-2]},
        {"batch-p1.jpg": full},
        {"batch-p1.jpg": full, "batch-p2.jpg": full[:-4], "batch.status": b"ok:2"},
        {"batch-p1.jpg": full, "batch-p2.jpg": full, "batch.status": b"ok:2"},
    ], check)
    app = coordinator(store, fake)
    app.collect(sid, "batch")
    assert observations[1] == (1, [], "scanning")
    assert observations[2] == (2, [1], "scanning")
    assert observations[3] == (3, [1], "scanning")
    assert store.get(sid)["state"] == "done"
    assert store.get(sid)["pages"] == [1, 2]
    assert fake.writes == [(REQ_DIR + "/batch.json", {"src": "scan-station", "dpi": 300, "duplex": True})]
    assert fake.closed and app.active_id is None


def test_terminal_count_waits_for_page_absent_from_initial_listing(tmp_path):
    store, sid = setup_batch(tmp_path)
    fake = FakeSMB([
        {"batch-p1.jpg": jpeg(), "batch.status": b"ok:2"},
        {"batch-p1.jpg": jpeg(), "batch.status": b"ok:2"},
        {"batch-p1.jpg": jpeg(), "batch-p2.jpg": jpeg("blue"), "batch.status": b"ok:2"},
    ])
    app = coordinator(store, fake)
    app.collect(sid, "batch")
    assert store.get(sid)["pages"] == [1, 2]
    assert store.get(sid)["state"] == "done"


def test_error_keeps_received_pages_and_closes_connection(tmp_path):
    store, sid = setup_batch(tmp_path)
    fake = FakeSMB([{"batch-p1.jpg": jpeg(), "batch.status": b"error: paper jam"}])
    app = coordinator(store, fake)
    app.collect(sid, "batch")
    assert store.get(sid)["state"] == "error"
    assert store.get(sid)["pages"] == [1]
    assert "paper jam" in store.get(sid)["msg"]
    assert fake.closed and app.active_id is None


def test_timeout_does_not_release_device_before_terminal_status(tmp_path):
    store, sid = setup_batch(tmp_path)
    fake = FakeSMB([{"batch-p1.jpg": jpeg()}])
    app = coordinator(store, fake, limit=5, timeout=20)
    app.collect(sid, "batch")
    assert store.get(sid)["state"] == "error"
    assert store.get(sid)["pages"] == [1]
    assert app.active_id == sid
    with pytest.raises(ScanBusy):
        app.start(name="must not overlap")
    assert fake.closed


def test_progress_resets_inactivity_timeout_for_long_batch(tmp_path):
    store, sid = setup_batch(tmp_path)
    fake = FakeSMB([
        {}, {"batch-p1.jpg": jpeg()}, {"batch-p1.jpg": jpeg()},
        {"batch-p1.jpg": jpeg(), "batch-p2.jpg": jpeg()},
        {"batch-p1.jpg": jpeg(), "batch-p2.jpg": jpeg(), "batch.status": b"ok:2"},
    ])
    app = coordinator(store, fake, timeout=25)
    app.collect(sid, "batch")
    assert app.stop.waits >= 4  # Total duration exceeds the inactivity window.
    assert store.get(sid)["state"] == "done"


def test_restart_keeps_busy_and_never_reissues_submitted_request(tmp_path):
    store, sid = setup_batch(tmp_path)
    store.update_batch(sid, "batch", dispatch="dispatching")
    store.add_page(sid, "batch", 1, jpeg())
    store.delete_page(sid, 1)
    recovered = DocumentStore(tmp_path, processor=lambda data: (data, {}))
    fake = FakeSMB([{"batch-p1.jpg": jpeg(), "batch-p2.jpg": jpeg(), "batch.status": b"ok:2"}])
    app = coordinator(recovered, fake)
    assert app.active_id == sid
    with pytest.raises(ScanBusy):
        app.start()
    app.collect(sid, "batch")
    assert fake.writes == []
    assert recovered.get(sid)["pages"] == [2]
    assert app.active_id is None


def test_incomplete_terminal_page_times_out_with_partial_results(tmp_path):
    store, sid = setup_batch(tmp_path)
    fake = FakeSMB([{"batch-p1.jpg": jpeg(), "batch-p2.jpg": jpeg()[:-20], "batch.status": b"ok:2"}])
    app = coordinator(store, fake, timeout=20)
    app.collect(sid, "batch")
    assert store.get(sid)["state"] == "error"
    assert store.get(sid)["pages"] == [1]
    assert app.active_id is None


def test_transient_list_failure_does_not_finalize_empty_batch(tmp_path):
    store, sid = setup_batch(tmp_path)
    fake = FakeSMB([OSError("temporary SMB failure"),
                    {"batch-p1.jpg": jpeg(), "batch.status": b"ok"}])
    app = coordinator(store, fake)
    app.collect(sid, "batch")
    assert store.get(sid)["pages"] == [1]
    assert len(fake.writes) == 1


def test_request_is_invisible_to_agent_until_upload_completes(tmp_path):
    store, sid = setup_batch(tmp_path)

    class ObservePublication(FakeSMB):
        def putFile(self, share, path, callback):
            assert store.batch(sid, "batch")["dispatch"] == "prepared"
            assert not path.endswith(".json")
            super().putFile(share, path, callback)
            assert self.writes == []  # Agent cannot consume a partial request.

        def rename(self, share, source, destination):
            assert store.batch(sid, "batch")["dispatch"] == "dispatching"
            super().rename(share, source, destination)

    fake = ObservePublication([{"batch-p1.jpg": jpeg(), "batch.status": b"ok:1"}])
    coordinator(store, fake).collect(sid, "batch")
    assert len(fake.writes) == 1
    assert store.get(sid)["state"] == "done"


def test_failed_unpublished_upload_releases_device_without_request(tmp_path):
    store, sid = setup_batch(tmp_path)

    class FailedUpload(FakeSMB):
        def putFile(self, share, path, callback):
            assert path.endswith(".upload")
            raise OSError("upload interrupted")

    fake = FailedUpload([{}])
    app = coordinator(store, fake)
    app.collect(sid, "batch")
    assert fake.writes == []
    assert app.active_id is None
    assert store.get(sid)["state"] == "error"


def test_ambiguous_publish_response_is_never_reissued(tmp_path):
    store, sid = setup_batch(tmp_path)

    class LostReply(FakeSMB):
        def rename(self, share, source, destination):
            super().rename(share, source, destination)
            raise OSError("reply lost after rename")

    fake = LostReply([{"batch-p1.jpg": jpeg(), "batch.status": b"ok:1"}])
    coordinator(store, fake).collect(sid, "batch")
    assert len(fake.writes) == 1
    assert store.get(sid)["state"] == "done"


def rescan_batch(tmp_path):
    store, sid = setup_batch(tmp_path, dpi=300, duplex=True)
    store.add_page(sid, "batch", 1, jpeg("red"))
    store.add_page(sid, "batch", 2, jpeg("green"))
    store.finish_batch(sid, "batch")
    store.start_batch(sid, "rescan", target_page=2)
    return store, sid


def test_rescan_request_is_single_simplex_and_replacement_waits_for_terminal(tmp_path):
    store, sid = rescan_batch(tmp_path)
    observations = []

    def before_list(index):
        if index == 1:
            observations.append((store.page_bytes(sid, 2), store.get(sid)["page_details"]["2"]["revision"]))

    fake = FakeSMB([{"rescan-p1.jpg": jpeg("blue")},
                    {"rescan-p1.jpg": jpeg("blue"), "rescan.status": b"ok:1"}], before_list)
    coordinator(store, fake).collect(sid, "rescan")
    assert observations == [(jpeg("green"), 1)]
    assert fake.writes == [(REQ_DIR + "/rescan.json", {"src": "scan-station", "dpi": 300,
                                                      "duplex": False, "max_pages": 1})]
    assert store.get(sid)["pages"] == [1, 2]
    assert store.page_bytes(sid, 2) == jpeg("blue")
    assert store.get(sid)["page_details"]["2"]["revision"] == 2


@pytest.mark.parametrize("terminal", [b"ok:0", b"ok", b"ok:2", b"error pages=1: jam", b"error: failed"])
def test_rescan_unsuccessful_terminal_keeps_original(tmp_path, terminal):
    store, sid = rescan_batch(tmp_path)
    fake = FakeSMB([{"rescan-p1.jpg": jpeg("blue"), "rescan.status": terminal}])
    app = coordinator(store, fake, timeout=20)
    app.collect(sid, "rescan")
    assert store.page_bytes(sid, 2) == jpeg("green")
    assert store.get(sid)["page_details"]["2"]["revision"] == 1
    assert store.get(sid)["state"] == "error" and app.active_id is None


def test_rescan_restart_recovers_staged_page_without_request_or_old_page_loss(tmp_path):
    store, sid = rescan_batch(tmp_path)
    store.update_batch(sid, "rescan", dispatch="submitted")
    store.add_page(sid, "rescan", 1, jpeg("blue"))
    restored = DocumentStore(tmp_path, processor=lambda data: (data, {}))
    assert restored.page_bytes(sid, 2) == jpeg("green")
    fake = FakeSMB([{"rescan.status": b"ok:1"}])
    app = coordinator(restored, fake)
    with pytest.raises(ScanBusy):
        app.rescan(sid, 1)
    app.collect(sid, "rescan")
    assert fake.writes == [] and restored.page_bytes(sid, 2) == jpeg("blue")


def test_interrupted_rescan_keeps_old_page_and_device_reserved(tmp_path):
    store, sid = rescan_batch(tmp_path)
    fake = FakeSMB([{"rescan-p1.jpg": jpeg("blue")[:-2]}])
    app = coordinator(store, fake, limit=5, timeout=20)
    app.collect(sid, "rescan")
    assert store.page_bytes(sid, 2) == jpeg("green")
    assert app.active_id == sid and store.get(sid)["state"] == "error"


def test_counted_error_preserves_published_pages_and_reports_error(tmp_path):
    assert terminal_result("error pages=1: paper jam")["count"] == 1
    store, sid = setup_batch(tmp_path)
    fake = FakeSMB([{"batch-p1.jpg": jpeg(), "batch.status": b"error pages=1: paper jam"}])
    coordinator(store, fake).collect(sid, "batch")
    assert store.get(sid)["pages"] == [1]
    assert store.get(sid)["state"] == "error" and "paper jam" in store.get(sid)["msg"]
