import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest
from PIL import Image

from documents import DocumentConflict, DocumentStore, atomic_write, complete_jpeg, scan_options


def jpeg(color="white", size=(60, 90)):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, "JPEG")
    return output.getvalue()


def passthrough(data):
    return data, {"method": "test-preserve"}


def test_persistent_append_delete_does_not_overwrite_or_resurrect(tmp_path):
    store = DocumentStore(tmp_path, processor=passthrough)
    sid = store.create("示例文件甲")["id"]
    first = store.start_batch(sid, "batch1", dpi=150)
    store.add_page(sid, first["id"], 2, jpeg("blue"))
    store.add_page(sid, first["id"], 1, jpeg("red"))
    assert store.get(sid)["pages"] == [1, 2]
    store.delete_page(sid, 2)
    store.add_page(sid, first["id"], 2, jpeg("green"))
    assert store.get(sid)["pages"] == [1]
    store.finish_batch(sid, first["id"])
    store.rename(sid, "示例文件最终稿")
    second = store.start_batch(sid, "batch2", dpi=300, duplex=True)
    assert store.add_page(sid, second["id"], 1, jpeg("yellow")) == 3
    store.finish_batch(sid, second["id"])

    restored = DocumentStore(tmp_path, processor=passthrough)
    document = restored.get(sid)
    assert document["name"] == "示例文件最终稿"
    assert document["pages"] == [1, 3]
    assert document["page_details"]["1"]["dpi"] == 150
    assert document["page_details"]["3"]["dpi"] == 300
    assert document["duplex"] is True
    assert restored.page_bytes(sid, 1, original=True) == jpeg("red")
    assert (tmp_path / sid / "raw/p2.jpg").read_bytes() == jpeg("blue")


def test_restart_recovers_raw_page_if_processing_was_interrupted(tmp_path):
    def interrupted(data):
        raise RuntimeError("power loss after raw save")

    store = DocumentStore(tmp_path, processor=interrupted)
    sid = store.create("可恢复文件")["id"]
    store.start_batch(sid, "batch-recovery", dpi=200)
    with pytest.raises(RuntimeError):
        store.add_page(sid, "batch-recovery", 1, jpeg())
    assert store.get(sid)["pages"] == []
    recovered = DocumentStore(tmp_path, processor=passthrough)
    assert recovered.get(sid)["pages"] == [1]
    assert recovered.get(sid)["state"] == "scanning"
    assert recovered.pending_batches()[0][1]["source_pages"] == {"1": 1}
    recovered.add_page(sid, "batch-recovery", 1, jpeg("black"))
    assert recovered.page_bytes(sid, 1) == jpeg()


def test_migration_is_lossless_and_repeatable_even_after_delete(tmp_path):
    legacy = tmp_path / "legacy"
    source = legacy / "s20000101-120000-demo"
    source.mkdir(parents=True)
    (source / "p1.jpg").write_bytes(jpeg("red"))
    (source / "p3.jpg").write_bytes(jpeg("blue"))
    (source / "p4.jpg").write_bytes(b"incomplete JPEG")
    store = DocumentStore(tmp_path / "documents", processor=passthrough, legacy_root=legacy)
    document = store.list()[0]
    assert document["pages"] == [1, 3]
    assert store.page_bytes(document["id"], 1, original=True) == jpeg("red")
    assert (store.root / document["id"] / "raw/p4.jpg").read_bytes() == b"incomplete JPEG"
    store.delete_page(document["id"], 1)
    store = DocumentStore(store.root, processor=passthrough, legacy_root=legacy)
    assert store.list()[0]["pages"] == [3]
    assert (source / "p1.jpg").read_bytes() == jpeg("red")
    batch = store.start_batch(document["id"], "append")
    assert store.add_page(document["id"], batch["id"], 1, jpeg()) == 5


def test_migration_processes_preview_but_preserves_original_and_handles_failure(tmp_path):
    legacy = tmp_path / "legacy/s20260912-example"
    legacy.mkdir(parents=True)
    original = jpeg("red", (80, 100))
    (legacy / "p1.jpg").write_bytes(original)
    (legacy / "p2.jpg").write_bytes(jpeg("blue"))

    def crop_or_fail(data):
        if data == original:
            return jpeg("red", (60, 80)), {"method": "corrected"}
        raise RuntimeError("test processor failure")

    store = DocumentStore(tmp_path / "documents", processor=crop_or_fail, legacy_root=legacy.parent)
    document = store.list()[0]
    assert document["pages"] == [1, 2]
    assert store.page_bytes(document["id"], 1) != original
    assert store.page_bytes(document["id"], 1, original=True) == original
    assert (legacy / "p1.jpg").read_bytes() == original
    assert document["page_details"]["1"]["crop"]["method"] == "corrected"
    assert store.page_bytes(document["id"], 2) == jpeg("blue")
    assert document["page_details"]["2"]["crop"]["processing_error"] == "RuntimeError"


def test_pdf_page_sizes_and_order_follow_each_page_dpi(tmp_path):
    fitz = pytest.importorskip("fitz")
    store = DocumentStore(tmp_path, processor=passthrough)
    sid = store.create("混合分辨率")["id"]
    store.start_batch(sid, "one", dpi=150)
    store.add_page(sid, "one", 1, jpeg("red", (300, 450)))
    store.finish_batch(sid, "one")
    store.start_batch(sid, "two", dpi=300)
    store.add_page(sid, "two", 1, jpeg("blue", (300, 450)))
    store.finish_batch(sid, "two")
    data, name = store.pdf(sid)
    assert name == "混合分辨率"
    with fitz.open(stream=data, filetype="pdf") as pdf:
        assert len(pdf) == 2
        assert tuple(pdf[0].rect)[2:] == pytest.approx((144, 216))
        assert tuple(pdf[1].rect)[2:] == pytest.approx((72, 108))
        first_pixel = pdf[0].get_pixmap().samples[:3]
        second_pixel = pdf[1].get_pixmap().samples[:3]
        assert first_pixel[0] > 240 and first_pixel[2] < 20
        assert second_pixel[2] > 240 and second_pixel[0] < 20


@pytest.mark.parametrize("dpi", [True, False, 149, 600, "150", 150.0, None])
def test_dpi_is_finite_and_strict(dpi):
    with pytest.raises(ValueError):
        scan_options(dpi)


@pytest.mark.parametrize("duplex", ["false", 0, 1, None])
def test_duplex_is_boolean(duplex):
    with pytest.raises(ValueError):
        scan_options(150, duplex)


def test_partial_jpeg_is_never_published(tmp_path):
    store = DocumentStore(tmp_path, processor=passthrough)
    sid = store.create()["id"]
    store.start_batch(sid, "batch")
    with pytest.raises(ValueError):
        store.add_page(sid, "batch", 1, jpeg()[:-2])
    assert store.get(sid)["pages"] == []
    assert not (tmp_path / sid / "raw/p1.jpg").exists()


@pytest.mark.parametrize("sid", ["..", "../outside", "/etc", "a/b", "a\\b", "%2e%2e"])
def test_document_path_cannot_escape_root(tmp_path, sid):
    store = DocumentStore(tmp_path, processor=passthrough)
    with pytest.raises(KeyError):
        store.get(sid)


def ordered_document(tmp_path):
    store = DocumentStore(tmp_path, processor=passthrough)
    sid = store.create("稳定文件名")["id"]
    store.start_batch(sid, "original", dpi=300)
    for number, color in enumerate(("red", "green", "blue"), 1):
        store.add_page(sid, "original", number, jpeg(color))
    store.finish_batch(sid, "original")
    return store, sid


def test_delete_document_persists_tombstone_and_hides_all_public_access(tmp_path):
    store, sid = ordered_document(tmp_path)
    snapshot = store.get(sid)
    image_paths = list((tmp_path / sid).rglob("*.jpg"))
    images = {path: path.read_bytes() for path in image_paths}
    assert store.delete_document(sid, updated_at=snapshot["updated_at"]) == {"ok": True, "id": sid}
    metadata = json.loads((tmp_path / sid / "document.json").read_text())
    assert metadata["deleted_at"]
    assert {path: path.read_bytes() for path in image_paths} == images
    for current in (store, DocumentStore(tmp_path, processor=passthrough)):
        assert current.list() == []
        assert current.pending_batches() == [] and current.pending_ocr() == []
        assert current.reprocess_pages() == []
        operations = (
            lambda: current.get(sid), lambda: current.page_bytes(sid, 1),
            lambda: current.page_bytes(sid, 1, original=True), lambda: current.pdf(sid),
            lambda: current.rename(sid, "不可改名"), lambda: current.lock_name(sid),
            lambda: current.delete_page(sid, 1), lambda: current.start_batch(sid),
            lambda: current.reorder(sid, [3, 2, 1], snapshot["order_revision"]),
            lambda: current.analyze_blank_pages(sid), lambda: current.reprocess_pages(sid),
            lambda: current.delete_document(sid, updated_at=snapshot["updated_at"]),
        )
        for operation in operations:
            with pytest.raises(KeyError):
                operation()
    assert store.create(snapshot["name"])["name"] == snapshot["name"]


def test_delete_document_rejects_own_pending_scan_but_allows_other_document(tmp_path):
    store, sid = ordered_document(tmp_path)
    store.start_batch(sid, "pending")
    store.waiting(sid, "pending", "仍在等待扫描结束")
    with pytest.raises(DocumentConflict):
        store.delete_document(sid, updated_at=store.get(sid)["updated_at"])
    store.finish_batch(sid, "pending")
    another = store.create("另一个正在扫描的文件")["id"]
    store.start_batch(another, "another-batch")
    assert store.delete_document(sid, updated_at=store.get(sid)["updated_at"])["ok"] is True
    assert store.pending_batches()[0][0] == another


def test_delete_document_rejects_stale_snapshot_even_when_clock_stops(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 13, tzinfo=timezone.utc)

    monkeypatch.setattr("documents.datetime", FrozenDatetime)
    store = DocumentStore(tmp_path, processor=passthrough)
    sid = store.create("原名")["id"]
    original = store.get(sid)
    renamed = store.rename(sid, "新名")
    assert renamed["updated_at"] > original["updated_at"]
    with pytest.raises(DocumentConflict):
        store.delete_document(sid, updated_at=original["updated_at"])
    assert store.get(sid)["name"] == "新名"
    restored = DocumentStore(tmp_path, processor=passthrough)
    latest = restored.rename(sid, "重启后改名")
    assert latest["updated_at"] > renamed["updated_at"]
    with pytest.raises(DocumentConflict):
        restored.delete_document(sid, updated_at=renamed["updated_at"])
    assert restored.delete_document(sid, updated_at=latest["updated_at"])["ok"] is True


def test_delete_document_save_failure_preserves_memory_and_disk(tmp_path, monkeypatch):
    store, sid = ordered_document(tmp_path)
    before = store.get(sid)
    metadata_path = tmp_path / sid / "document.json"
    original_bytes = metadata_path.read_bytes()
    original_metadata = json.loads(original_bytes)

    def failed_write(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr("documents.atomic_write", failed_write)
    with pytest.raises(OSError, match="simulated disk failure"):
        store.delete_document(sid, updated_at=before["updated_at"])
    assert store.get(sid) == before
    assert store._documents[sid] == original_metadata
    assert metadata_path.read_bytes() == original_bytes


def test_delete_document_cannot_be_restored_by_legacy_import_or_recovery(tmp_path):
    source = tmp_path / "legacy" / "s-deleted-history"
    source.mkdir(parents=True)
    (source / "p1.jpg").write_bytes(jpeg())
    store = DocumentStore(tmp_path / "documents", processor=passthrough, legacy_root=source.parent)
    sid = source.name
    store.delete_document(sid, updated_at=store.get(sid)["updated_at"])
    before = (store.directory(sid) / "document.json").read_bytes()
    # A missing preview and new legacy image would normally trigger recovery/import.
    (store.directory(sid) / "p1.jpg").unlink()
    (source / "p2.jpg").write_bytes(jpeg("blue"))
    restored = DocumentStore(store.root, processor=lambda data: pytest.fail("Deleted file was processed"),
                             legacy_root=source.parent)
    assert restored.list() == []
    assert (store.directory(sid) / "document.json").read_bytes() == before
    assert not (store.directory(sid) / "p1.jpg").exists()
    assert not (store.directory(sid) / "raw/p2.jpg").exists()


def test_delete_document_cancels_running_and_queued_ocr_without_stopping_worker(tmp_path):
    from ocr_naming import OCRNamingWorker

    store = DocumentStore(tmp_path, processor=passthrough)
    ids = []
    for number in range(3):
        sid = store.create(auto_name=True)["id"]
        batch = store.start_batch(sid)
        store.add_page(sid, batch["id"], 1, jpeg())
        store.finish_batch(sid, batch["id"])
        ids.append(sid)
    started, release, healthy = threading.Event(), threading.Event(), threading.Event()
    recognized = []

    def recognizer(path):
        recognized.append(path.parent.name)
        if path.parent.name == ids[0]:
            started.set()
            assert release.wait(3)
        else:
            healthy.set()
        return "识别结果"

    worker = OCRNamingWorker(store, recognizer=recognizer)
    worker.start()
    try:
        worker.submit(ids[0])
        assert started.wait(2)
        worker.submit(ids[1])
        for sid in ids[:2]:
            store.delete_document(sid, updated_at=store.get(sid)["updated_at"])
            assert store.ocr_eligible(sid) is False
            assert store.begin_ocr(sid) is None
            assert store.finish_ocr(sid, "删除后回写") is None
        worker.submit(ids[2])
        release.set()
        assert healthy.wait(2)
        assert recognized == [ids[0], ids[2]]
        assert worker.thread.is_alive()
        assert [d["id"] for d in store.list()] == [ids[2]]
        for sid in ids[:2]:
            persisted = json.loads((store.directory(sid) / "document.json").read_text())
            assert persisted["deleted_at"] and persisted["ocr_state"] == "cancelled"
            assert persisted["name_source"] == "default"
    finally:
        release.set()
        worker.close()
        worker.thread.join(timeout=2)


def test_explicit_order_survives_pdf_reload_append_and_delete(tmp_path):
    fitz = pytest.importorskip("fitz")
    store, sid = ordered_document(tmp_path)
    reordered = store.reorder(sid, [3, 1, 2], store.get(sid)["order_revision"])
    assert reordered["pages"] == [3, 1, 2]
    data, _ = store.pdf(sid)
    with fitz.open(stream=data, filetype="pdf") as pdf:
        pixels = [page.get_pixmap().samples[:3] for page in pdf]
        assert pixels[0][2] > 240 and pixels[1][0] > 240 and pixels[2][1] > 100
    store = DocumentStore(tmp_path, processor=passthrough)
    assert store.get(sid)["pages"] == [3, 1, 2]
    store.start_batch(sid, "appended")
    store.add_page(sid, "appended", 2, jpeg("white"))
    store.add_page(sid, "appended", 1, jpeg("black"))
    store.finish_batch(sid, "appended")
    assert store.get(sid)["pages"] == [3, 1, 2, 4, 5]
    store.delete_page(sid, 1)
    assert store.get(sid)["pages"] == [3, 2, 4, 5]
    assert store.get(sid)["page_details"]["4"]["source_page"] == 1


def test_reorder_rejects_stale_or_incomplete_permutations_and_active_scan(tmp_path):
    store, sid = ordered_document(tmp_path)
    revision = store.get(sid)["order_revision"]
    for bad in ([1, 2], [1, 2, 2], [1, 2, 9], [True, 2, 3], ["1", 2, 3]):
        with pytest.raises(ValueError):
            store.reorder(sid, bad, revision)
    store.reorder(sid, [3, 2, 1], revision)
    with pytest.raises(DocumentConflict):
        store.reorder(sid, [1, 2, 3], revision)
    other = store.create("其他文件")["id"]
    store.start_batch(other, "busy")
    with pytest.raises(DocumentConflict):
        store.reorder(sid, [1, 2, 3], store.get(sid)["order_revision"])


def test_rescan_switches_fixed_slot_only_after_success_and_keeps_old_revisions(tmp_path):
    store, sid = ordered_document(tmp_path)
    store.reorder(sid, [3, 1, 2], store.get(sid)["order_revision"])
    original = store.page_bytes(sid, 1)
    name = store.get(sid)["name"]
    batch = store.start_batch(sid, "rescan1", target_page=1)
    assert batch["dpi"] == 300 and batch["duplex"] is False and batch["max_pages"] == 1
    assert store.get(sid)["rescan_page"] == 1
    assert store.add_page(sid, "rescan1", 1, jpeg("yellow")) == 1
    assert store.page_bytes(sid, 1) == original
    assert store.page_bytes(sid, 1, original=True) == original
    store.finish_batch(sid, "rescan1")
    document = store.get(sid)
    assert document["pages"] == [3, 1, 2] and document["name"] == name
    assert document["page_details"]["1"]["revision"] == 2
    assert store.page_bytes(sid, 1) == jpeg("yellow")
    assert store.page_bytes(sid, 1, original=True) == jpeg("yellow")
    history = document["page_details"]["1"]["revisions"]
    assert (store.directory(sid) / history[0]["file"]).read_bytes() == original
    assert (store.directory(sid) / history[0]["raw_file"]).read_bytes() == original
    fitz = pytest.importorskip("fitz")
    pdf_data, _ = store.pdf(sid)
    with fitz.open(stream=pdf_data, filetype="pdf") as pdf:
        replaced_pixel = pdf[1].get_pixmap().samples[:3]
        assert replaced_pixel[0] > 240 and replaced_pixel[1] > 240 and replaced_pixel[2] < 20
    store = DocumentStore(tmp_path, processor=passthrough)
    assert store.page_bytes(sid, 1) == jpeg("yellow")
    store.start_batch(sid, "append")
    assert store.add_page(sid, "append", 1, jpeg()) == 4


@pytest.mark.parametrize("stage,error", [(False, ""), (True, "卡纸"), (False, "连接失败")])
def test_failed_or_empty_rescan_retains_page_slot_name_and_raw(tmp_path, stage, error):
    store, sid = ordered_document(tmp_path)
    before = store.get(sid)
    original = store.page_bytes(sid, 2)
    store.start_batch(sid, "rescan", target_page=2)
    if stage:
        store.add_page(sid, "rescan", 1, jpeg("yellow"))
    store.finish_batch(sid, "rescan", error=error)
    after = store.get(sid)
    assert after["state"] == "error"
    assert after["pages"] == before["pages"] and after["name"] == before["name"]
    assert store.page_bytes(sid, 2) == original
    assert store.page_bytes(sid, 2, original=True) == original
    assert after["page_details"]["2"]["revision"] == 1


def test_rescan_metadata_failure_never_publishes_half_replacement(tmp_path, monkeypatch):
    store, sid = ordered_document(tmp_path)
    original = store.page_bytes(sid, 2)
    store.start_batch(sid, "rescan", target_page=2)
    store.add_page(sid, "rescan", 1, jpeg("yellow"))
    save = store._save

    def fail_commit(document):
        if document["page_details"]["2"]["revision"] == 2:
            raise OSError("disk failure at commit")
        save(document)

    monkeypatch.setattr(store, "_save", fail_commit)
    with pytest.raises(OSError):
        store.finish_batch(sid, "rescan")
    assert store.page_bytes(sid, 2) == original
    restored = DocumentStore(tmp_path, processor=passthrough)
    assert restored.page_bytes(sid, 2) == original
    assert restored.pending_batches()[0][1]["target_page"] == 2
    restored.finish_batch(sid, "rescan")
    assert restored.page_bytes(sid, 2) == jpeg("yellow")


def test_rescan_never_restarts_ocr_or_changes_filename_and_blocks_delete(tmp_path):
    store = DocumentStore(tmp_path, processor=passthrough)
    sid = store.create(auto_name=True)["id"]
    store.start_batch(sid, "original")
    store.add_page(sid, "original", 1, jpeg())
    store.finish_batch(sid, "original")
    name = store.get(sid)["name"]
    store.begin_ocr(sid)
    store.start_batch(sid, "rescan", target_page=1)
    assert store.get(sid)["ocr_state"] == "cancelled"
    with pytest.raises(DocumentConflict):
        store.delete_page(sid, 1)
    called = []
    store.on_page_published = lambda *args: called.append(args)
    store.add_page(sid, "rescan", 1, jpeg("blue"))
    store.finish_batch(sid, "rescan")
    store.finish_ocr(sid, "迟到OCR")
    assert store.get(sid)["name"] == name and called == []


def test_rescan_processing_failure_and_extra_page_cannot_damage_original(tmp_path):
    store, sid = ordered_document(tmp_path)
    original = store.page_bytes(sid, 1)
    store.start_batch(sid, "rescan", target_page=1)

    def broken_processor(data):
        raise RuntimeError("processing interrupted")

    store.processor = broken_processor
    with pytest.raises(RuntimeError):
        store.add_page(sid, "rescan", 1, jpeg("yellow"))
    with pytest.raises(ValueError):
        store.add_page(sid, "rescan", 2, jpeg("yellow"))
    assert store.page_bytes(sid, 1) == original
    restored = DocumentStore(tmp_path, processor=passthrough)
    assert restored.page_bytes(sid, 1) == original
    restored.finish_batch(sid, "rescan", error="处理失败")
    assert restored.get(sid)["page_details"]["1"]["revision"] == 1


def test_old_metadata_gains_capture_order_and_page_revisions_without_renumbering(tmp_path):
    store, sid = ordered_document(tmp_path)
    store.delete_page(sid, 2)
    path = store.directory(sid) / "document.json"
    metadata = json.loads(path.read_text())
    del metadata["page_order"]
    del metadata["order_revision"]
    for page in metadata["page_details"].values():
        del page["revision"]
    path.write_text(json.dumps(metadata))
    restored = DocumentStore(tmp_path, processor=passthrough)
    document = restored.get(sid)
    assert document["pages"] == [1, 3] and document["order_revision"] == 0
    assert document["page_details"]["3"]["revision"] == 1


def test_reorder_does_not_lose_page_waiting_for_recovery(tmp_path):
    store, sid = ordered_document(tmp_path)
    store.start_batch(sid, "incomplete")

    def interrupted(data):
        raise RuntimeError("power interruption after saving raw")

    store.processor = interrupted
    with pytest.raises(RuntimeError):
        store.add_page(sid, "incomplete", 1, jpeg("yellow"))
    store.finish_batch(sid, "incomplete", error="processing interrupted")
    store.reorder(sid, [3, 1, 2], store.get(sid)["order_revision"])
    restored = DocumentStore(tmp_path, processor=passthrough)
    assert restored.get(sid)["pages"] == [3, 1, 2, 4]
    assert restored.page_bytes(sid, 4) == jpeg("yellow")


def test_offline_reprocess_uses_current_raw_and_preserves_names_order_ocr_timestamp(tmp_path):
    store, sid = ordered_document(tmp_path)
    store.reorder(sid, [3, 1, 2], store.get(sid)["order_revision"])
    store.start_batch(sid, "rescan", target_page=2)
    store.add_page(sid, "rescan", 1, jpeg("yellow"))
    store.finish_batch(sid, "rescan")
    before = store.get(sid)
    old_processed = {n: store.page_bytes(sid, n) for n in before["pages"]}
    raw_inputs = []

    def version_two(raw):
        raw_inputs.append(raw)
        return jpeg("white", (50, 70)), {"processing_version": 2, "cropped": True}

    store.processor = version_two
    report = store.reprocess_pages(sid)
    assert all(page["action"] == "reprocessed" for page in report)
    assert raw_inputs == [jpeg("blue"), jpeg("red"), jpeg("yellow")]
    after = store.get(sid)
    for key in ("name", "name_source", "auto_name", "ocr_state", "pages", "updated_at", "order_revision"):
        assert after[key] == before[key]
    assert after["page_details"]["2"]["revision"] == 3
    assert store.page_bytes(sid, 2, original=True) == jpeg("yellow")
    for n in before["pages"]:
        previous = after["page_details"][str(n)]["revisions"][-1]
        assert (store.directory(sid) / previous["file"]).read_bytes() == old_processed[n]
    assert all(page["action"] == "skip" for page in store.reprocess_pages(sid))


def test_offline_reprocess_dry_run_never_writes_even_old_metadata_and_filters_id(tmp_path):
    store, sid = ordered_document(tmp_path)
    other = store.create("无关文件")["id"]
    metadata_path = store.directory(sid) / "document.json"
    old = json.loads(metadata_path.read_text())
    old.pop("page_order")
    old.pop("order_revision")
    old["ocr_state"] = "running"
    old["name_source"] = "default"
    metadata_path.write_text(json.dumps(old))
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / "tools/reprocess_documents.py"),
               "--data-dir", str(tmp_path), "--document-id", sid, "--dry-run"]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    report = json.loads(result.stdout)
    assert report["counts"]["would_reprocess"] == 3
    assert {p["id"] for p in report["pages"]} == {sid}
    after = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before
    loaded = DocumentStore(tmp_path, recover=False)
    assert loaded.get(sid)["ocr_state"] == "running"


def test_offline_reprocess_active_batch_and_failed_page_keep_existing_data(tmp_path):
    store, sid = ordered_document(tmp_path)
    before = store.page_bytes(sid, 1)
    store.start_batch(sid, "busy")
    with pytest.raises(DocumentConflict):
        store.reprocess_pages(sid, dry_run=True)
    store.finish_batch(sid, "busy", error="测试结束")

    def broken(raw):
        raise RuntimeError("test failure")

    store.processor = broken
    result = store.reprocess_pages(sid)
    assert all(page["action"] == "failed" for page in result)
    assert store.page_bytes(sid, 1) == before
    assert store.get(sid)["page_details"]["1"]["revision"] == 1


def test_atomic_write_persists_each_new_directory_entry(tmp_path, monkeypatch):
    synced = []
    real_fsync = os.fsync

    def track_fsync(fd):
        synced.append(Path(os.readlink(f"/proc/self/fd/{fd}")))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", track_fsync)
    target = tmp_path / "revisions/p1/r2-new/page.jpg"
    atomic_write(target, jpeg())
    required = [tmp_path, tmp_path / "revisions", tmp_path / "revisions/p1", target.parent]
    assert all(directory in synced for directory in required)
    assert [synced.index(directory) for directory in required] == sorted(synced.index(directory) for directory in required)
    assert target.read_bytes() == jpeg()


@pytest.mark.parametrize("damage", ["missing_directory", "bad_processed", "bad_raw"])
def test_restart_falls_back_to_last_complete_rescan_revision(tmp_path, damage):
    store, sid = ordered_document(tmp_path)
    original = store.page_bytes(sid, 2)
    store.start_batch(sid, "rescan", target_page=2)
    store.add_page(sid, "rescan", 1, jpeg("yellow"))
    store.finish_batch(sid, "rescan")
    page = store.get(sid)["page_details"]["2"]
    folder = store.directory(sid) / Path(page["file"]).parent
    if damage == "missing_directory":
        for path in folder.iterdir():
            path.unlink()
        folder.rmdir()
    elif damage == "bad_processed":
        (store.directory(sid) / page["file"]).write_bytes(b"truncated image")
    else:
        (store.directory(sid) / page["raw_file"]).write_bytes(b"truncated original")
    restored = DocumentStore(tmp_path, processor=passthrough)
    assert restored.get(sid)["pages"] == [1, 2, 3]
    assert restored.page_bytes(sid, 2) == original
    assert restored.page_bytes(sid, 2, original=True) == original
    metadata = restored.get(sid)["page_details"]["2"]
    assert metadata["revision"] == 3 and metadata["recovered_from_revision"] == 1
    assert DocumentStore(tmp_path, processor=passthrough).page_bytes(sid, 2) == original


def test_reprocess_revision_loss_recovers_previous_processed_and_current_raw(tmp_path):
    store, sid = ordered_document(tmp_path)
    original = store.page_bytes(sid, 1)
    store.processor = lambda data: (jpeg("yellow"), {"processing_version": 2})
    store.reprocess_pages(sid)
    page = store.get(sid)["page_details"]["1"]
    path = store.directory(sid) / page["file"]
    path.unlink()
    path.parent.rmdir()
    restored = DocumentStore(tmp_path, processor=passthrough)
    assert restored.page_bytes(sid, 1) == original
    assert restored.page_bytes(sid, 1, original=True) == original
    assert restored.get(sid)["page_details"]["1"]["revision"] == 3


def test_pending_ocr_reads_current_processed_pointer_after_offline_upgrade(tmp_path):
    store = DocumentStore(tmp_path, processor=passthrough)
    sid = store.create(auto_name=True)["id"]
    store.start_batch(sid, "original")
    store.add_page(sid, "original", 1, jpeg("red"))
    store.finish_batch(sid, "original")
    store.processor = lambda data: (jpeg("yellow"), {"processing_version": 2})
    store.reprocess_pages(sid)
    assert store.begin_ocr(sid).read_bytes() == jpeg("yellow")


def test_repeated_legacy_import_is_zero_write_and_preserves_history_position(tmp_path):
    source = tmp_path / "legacy/s20260101-legacy"
    source.mkdir(parents=True)
    (source / "p1.jpg").write_bytes(jpeg())
    store = DocumentStore(tmp_path / "documents", processor=passthrough, legacy_root=source.parent)
    sid = store.list()[0]["id"]
    path = store.directory(sid) / "document.json"
    metadata = json.loads(path.read_text())
    metadata.update(updated_at="2026-01-01T00:00:00Z", msg="保留原说明")
    path.write_text(json.dumps(metadata, ensure_ascii=False))
    original = path.read_bytes()
    modified = path.stat().st_mtime_ns

    restored = DocumentStore(store.root, processor=passthrough, legacy_root=source.parent)
    assert path.read_bytes() == original and path.stat().st_mtime_ns == modified
    assert restored.get(sid)["updated_at"] == "2026-01-01T00:00:00Z"
    assert restored.get(sid)["msg"] == "保留原说明"

    (source / "p2.jpg").write_bytes(jpeg("blue"))
    restored.migrate_legacy(source.parent)
    assert restored.get(sid)["pages"] == [1, 2]
    assert restored.get(sid)["updated_at"] != "2026-01-01T00:00:00Z"


def test_schema_upgrade_preserves_timestamp_of_existing_empty_error_document(tmp_path):
    store = DocumentStore(tmp_path, processor=passthrough)
    sid = store.create("空失败记录")["id"]
    store.start_batch(sid, "failed")
    store.finish_batch(sid, "failed", error="连接失败")
    path = store.directory(sid) / "document.json"
    metadata = json.loads(path.read_text())
    for field in ("name_source", "auto_name", "ocr_state", "page_order", "order_revision"):
        metadata.pop(field)
    metadata["updated_at"] = "2026-01-01T00:00:00Z"
    path.write_text(json.dumps(metadata))
    restored = DocumentStore(tmp_path, processor=passthrough)
    document = restored.get(sid)
    assert document["updated_at"] == "2026-01-01T00:00:00Z"
    assert document["name"] == "空失败记录" and document["msg"] == "连接失败"
    assert document["state"] == "error" and document["pages"] == []
    assert document["name_source"] == "manual" and document["order_revision"] == 0


def selection_snapshot(document):
    return {"order_revision": document["order_revision"],
            "page_revisions": {str(n): document["page_details"][str(n)]["revision"]
                               for n in document["pages"]}}


def test_blank_analysis_is_read_only_uses_current_version_and_retains_failures(tmp_path):
    store, sid = ordered_document(tmp_path)
    store.start_batch(sid, "replacement", target_page=2)
    store.add_page(sid, "replacement", 1, jpeg("white"))
    store.finish_batch(sid, "replacement")
    store.reorder(sid, [3, 2, 1], store.get(sid)["order_revision"])
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    seen = []

    def analyze(data):
        seen.append(data)
        if data == jpeg("blue"):
            raise ValueError("unreadable page")
        return {"is_blank": data == jpeg("white"), "version": 1, "reason": "fixture", "metrics": {}}

    store.blank_analyzer = analyze
    result = store.analyze_blank_pages(sid)
    assert result["document_id"] == sid and result["examined"] == 3
    assert result["candidates"] == [{"page": 2, "position": 2, "revision": 2}]
    assert result["errors"][0]["page"] == 3 and len(result["errors"]) == 1
    assert result["page_revisions"] == {"3": 1, "2": 2, "1": 1}
    assert seen == [jpeg("blue"), jpeg("white"), jpeg("red")]
    assert {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_blank_analysis_reads_and_classifies_outside_store_lock(tmp_path, monkeypatch):
    store, sid = ordered_document(tmp_path)
    read_bytes = Path.read_bytes
    phases = []

    def concurrent_read(phase):
        finished = threading.Event()
        worker = threading.Thread(target=lambda: (store.get(sid), finished.set()), daemon=True)
        worker.start()
        assert finished.wait(2), phase + " holds the document lock"
        worker.join()
        phases.append(phase)

    def read(path):
        concurrent_read("image read")
        return read_bytes(path)

    def analyze(data):
        concurrent_read("analysis")
        return {"is_blank": False}

    monkeypatch.setattr(Path, "read_bytes", read)
    store.blank_analyzer = analyze
    assert store.analyze_blank_pages(sid)["candidates"] == []
    assert phases.count("image read") == 3 and phases.count("analysis") == 3


@pytest.mark.parametrize("change", ["append", "rescan", "reorder", "delete", "begin_scan"])
def test_blank_analysis_rejects_changed_pages_or_new_active_batch(tmp_path, change):
    store, sid = ordered_document(tmp_path)
    changed = False

    def analyze(data):
        nonlocal changed
        if not changed:
            changed = True
            if change in ("append", "rescan", "begin_scan"):
                store.start_batch(sid, "concurrent", target_page=1 if change == "rescan" else None)
                if change != "begin_scan":
                    store.add_page(sid, "concurrent", 1, jpeg("white"))
                    store.finish_batch(sid, "concurrent")
            elif change == "reorder":
                store.reorder(sid, [3, 2, 1], store.get(sid)["order_revision"])
            else:
                store.delete_page(sid, 3)
        return {"is_blank": True}

    store.blank_analyzer = analyze
    with pytest.raises(DocumentConflict):
        store.analyze_blank_pages(sid)


def test_manual_bulk_delete_is_atomic_retains_files_and_undo_survives_restart_and_rename(tmp_path):
    store, sid = ordered_document(tmp_path)
    store.reorder(sid, [3, 1, 2], store.get(sid)["order_revision"])
    before = store.get(sid)
    images = {str(p): p.read_bytes() for p in tmp_path.rglob("*.jpg")}
    result = store.delete_selected(sid, [2, 3], **selection_snapshot(before))
    assert result["removed"] == [3, 2] and result["document"]["pages"] == [1]
    assert result["document"]["order_revision"] == before["order_revision"] + 1
    assert result["document"]["blank_undo"] == {"cleanup_id": result["cleanup_id"], "count": 2}
    assert result["document"]["name"] == before["name"]
    assert {str(p): p.read_bytes() for p in tmp_path.rglob("*.jpg")} == images
    with pytest.raises(KeyError):
        store.page_bytes(sid, 2)
    fitz = pytest.importorskip("fitz")
    with fitz.open(stream=store.pdf(sid)[0], filetype="pdf") as pdf:
        assert len(pdf) == 1 and pdf[0].get_pixmap().samples[0] > 240

    store = DocumentStore(tmp_path, processor=passthrough)
    store.rename(sid, "删除后重新命名")
    assert store.get(sid)["blank_undo"]["cleanup_id"] == result["cleanup_id"]
    undone = store.undo_blank_cleanup(sid, result["cleanup_id"])
    assert undone["restored"] == [3, 2] and undone["document"]["pages"] == [3, 1, 2]
    assert undone["document"]["name"] == "删除后重新命名"
    assert undone["document"]["blank_undo"] is None
    assert {str(p): p.read_bytes() for p in tmp_path.rglob("*.jpg")} == images
    assert all(undone["document"]["page_details"][str(n)]["revision"] == 1 for n in (1, 2, 3))
    with fitz.open(stream=store.pdf(sid)[0], filetype="pdf") as pdf:
        assert len(pdf) == 3 and pdf[0].get_pixmap().samples[2] > 240
    assert DocumentStore(tmp_path, processor=passthrough).get(sid)["pages"] == [3, 1, 2]
    with pytest.raises(DocumentConflict):
        store.undo_blank_cleanup(sid, result["cleanup_id"])


@pytest.mark.parametrize("bad", [[], [1, 1], [True], ["1"], [0], [-1], [4], None])
def test_bulk_delete_rejects_invalid_selection_without_metadata_change(tmp_path, bad):
    store, sid = ordered_document(tmp_path)
    before = (store.directory(sid) / "document.json").read_bytes()
    with pytest.raises(ValueError):
        store.delete_selected(sid, bad, **selection_snapshot(store.get(sid)))
    assert (store.directory(sid) / "document.json").read_bytes() == before


@pytest.mark.parametrize("bad", [None, {"1": True}, {"1": "1"}, {"01": 1}, {"-1": 1}])
def test_bulk_delete_requires_valid_complete_page_revision_snapshot(tmp_path, bad):
    store, sid = ordered_document(tmp_path)
    with pytest.raises(ValueError):
        store.delete_selected(sid, [1], order_revision=store.get(sid)["order_revision"], page_revisions=bad)
    with pytest.raises(DocumentConflict):
        store.delete_selected(sid, [1], order_revision=store.get(sid)["order_revision"], page_revisions={"1": 1})
    assert store.get(sid)["pages"] == [1, 2, 3]


@pytest.mark.parametrize("change", ["append", "rescan", "reorder", "delete", "reprocess"])
def test_bulk_delete_and_undo_reject_later_page_edits(tmp_path, change):
    store, sid = ordered_document(tmp_path)
    old = selection_snapshot(store.get(sid))
    result = store.delete_selected(sid, [3], **old)
    snapshot = selection_snapshot(result["document"])
    if change in ("append", "rescan"):
        store.start_batch(sid, "later", target_page=1 if change == "rescan" else None)
        store.add_page(sid, "later", 1, jpeg("white"))
        store.finish_batch(sid, "later")
    elif change == "reorder":
        store.reorder(sid, [2, 1], snapshot["order_revision"])
    elif change == "delete":
        store.delete_page(sid, 2)
    else:
        store.processor = lambda data: (data, {"processing_version": 2})
        store.reprocess_pages(sid)
    with pytest.raises(DocumentConflict):
        store.delete_selected(sid, [1], **snapshot)
    with pytest.raises(DocumentConflict):
        store.undo_blank_cleanup(sid, result["cleanup_id"])
    assert store.get(sid)["blank_undo"] is None
    assert 3 not in store.get(sid)["pages"]


def test_cleanup_and_undo_reject_any_active_batch_without_invoking_analyzer(tmp_path):
    store, sid = ordered_document(tmp_path)
    result = store.delete_selected(sid, [3], **selection_snapshot(store.get(sid)))
    other = store.create("正在扫描的其他文档")["id"]
    store.start_batch(other, "busy")
    store.blank_analyzer = lambda data: pytest.fail("Analyzer must not run during a scan")
    with pytest.raises(DocumentConflict):
        store.analyze_blank_pages(sid)
    with pytest.raises(DocumentConflict):
        store.delete_selected(sid, [1], **selection_snapshot(store.get(sid)))
    with pytest.raises(DocumentConflict):
        store.undo_blank_cleanup(sid, result["cleanup_id"])
    assert store.get(sid)["pages"] == [1, 2]


def test_bulk_delete_all_pages_cancels_ocr_and_undo_does_not_rename_or_retrigger_it(tmp_path):
    store = DocumentStore(tmp_path, processor=passthrough)
    sid = store.create(auto_name=True)["id"]
    store.start_batch(sid, "pages", duplex=True)
    store.add_page(sid, "pages", 1, jpeg())
    store.add_page(sid, "pages", 2, jpeg())
    store.finish_batch(sid, "pages")
    store.begin_ocr(sid)
    name = store.get(sid)["name"]
    result = store.delete_selected(sid, [1, 2], **selection_snapshot(store.get(sid)))
    assert result["document"]["pages"] == [] and result["document"]["ocr_state"] == "cancelled"
    assert store.finish_ocr(sid, "迟到的文字")["name"] == name
    with pytest.raises(ValueError):
        store.pdf(sid)
    restored = DocumentStore(tmp_path, processor=passthrough)
    undone = restored.undo_blank_cleanup(sid, result["cleanup_id"])
    assert undone["document"]["pages"] == [1, 2] and undone["document"]["name"] == name
    assert restored.pending_ocr() == []
    restored.start_batch(sid, "later")
    assert restored.add_page(sid, "later", 1, jpeg()) == 3


@pytest.mark.parametrize("operation", ["delete", "undo"])
def test_bulk_metadata_write_failure_preserves_memory_disk_and_retry(tmp_path, monkeypatch, operation):
    store, sid = ordered_document(tmp_path)
    result = None
    if operation == "undo":
        result = store.delete_selected(sid, [2], **selection_snapshot(store.get(sid)))
    before = store.get(sid)
    metadata = store.directory(sid) / "document.json"
    raw_metadata = metadata.read_bytes()
    save = store._save
    monkeypatch.setattr(store, "_save", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        if operation == "delete":
            store.delete_selected(sid, [2], **selection_snapshot(before))
        else:
            store.undo_blank_cleanup(sid, result["cleanup_id"])
    assert store.get(sid) == before and metadata.read_bytes() == raw_metadata
    assert DocumentStore(tmp_path, processor=passthrough).get(sid) == before
    monkeypatch.setattr(store, "_save", save)
    if operation == "delete":
        assert store.delete_selected(sid, [2], **selection_snapshot(before))["removed"] == [2]
    else:
        assert store.undo_blank_cleanup(sid, result["cleanup_id"])["restored"] == [2]


def test_simultaneous_bulk_deletes_cannot_both_commit_the_same_snapshot(tmp_path):
    store, sid = ordered_document(tmp_path)
    snapshot = selection_snapshot(store.get(sid))
    barrier = threading.Barrier(2)
    results = []

    def delete(number):
        barrier.wait(timeout=3)
        try:
            results.append(store.delete_selected(sid, [number], **snapshot))
        except DocumentConflict as exc:
            results.append(exc)

    workers = [threading.Thread(target=delete, args=(n,), daemon=True) for n in (1, 2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()
    successes = [r for r in results if isinstance(r, dict)]
    assert len(successes) == 1 and sum(isinstance(r, DocumentConflict) for r in results) == 1
    assert store.get(sid)["pages"] == successes[0]["document"]["pages"]
    assert len(store.get(sid)["pages"]) == 2
