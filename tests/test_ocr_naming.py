from concurrent.futures import ThreadPoolExecutor
import io
import json
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
from PIL import Image

from documents import DocumentStore
from ocr_naming import OCRNamingWorker, clean_filename, first_line_from_tsv, recognize_first_line
from server import ScanCoordinator


def jpeg(color="white"):
    buffer = io.BytesIO()
    Image.new("RGB", (600, 900), color).save(buffer, "JPEG")
    return buffer.getvalue()


def store_at(path):
    return DocumentStore(path, processor=lambda data: (data, {"method": "test"}))


def first_page(store, *, name=None, auto_name=True):
    document = store.create(name, auto_name=auto_name)
    sid = document["id"]
    batch = store.start_batch(sid)
    store.add_page(sid, batch["id"], 1, jpeg())
    store.finish_batch(sid, batch["id"])
    return sid


def await_state(store, sid, state):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        document = store.get(sid)
        if document["ocr_state"] == state:
            return document
        time.sleep(0.005)
    pytest.fail(f"OCR did not reach {state}: {store.get(sid)}")


TSV_HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"


def word(text, *, line=1, top=20, left=10, conf=90):
    return f"5\t1\t1\t1\t{line}\t1\t{left}\t{top}\t100\t20\t{conf}\t{text}\n"


def test_tsv_uses_visual_first_line_and_removes_filename_only_characters():
    tsv = TSV_HEADER + word("下面正文", line=2, top=60) + word("检测", left=10) + word("报告:甲/乙", left=130)
    assert first_line_from_tsv(tsv) == "检测报告甲乙"
    assert first_line_from_tsv(TSV_HEADER + word("noise", conf=5)) == ""
    assert first_line_from_tsv(TSV_HEADER) == ""
    assert clean_filename(' CON<>:"/\\|?* ') == "_CON"


@pytest.mark.parametrize("first,confidence", [("未识别清楚的标题", 20), ("字", 95)])
def test_uncertain_first_line_never_falls_through_to_body_text(first, confidence):
    tsv = TSV_HEADER + word(first, top=20, conf=confidence) + word("第二行正文", line=2, top=60)
    assert first_line_from_tsv(tsv) == ""


def test_tesseract_receives_read_only_top_crop_timeout_and_single_cpu(tmp_path):
    path = tmp_path / "page.jpg"
    path.write_bytes(jpeg())
    original = path.read_bytes()

    def fake_runner(args, **kwargs):
        assert args == ["tesseract", "stdin", "stdout", "-l", "chi_sim+eng", "--psm", "6", "tsv"]
        assert kwargs["timeout"] == 10 and kwargs["check"] is True
        assert kwargs["env"]["OMP_THREAD_LIMIT"] == "1"
        with Image.open(io.BytesIO(kwargs["input"])) as crop:
            assert crop.size == (1200, 630)
        return SimpleNamespace(stdout=(TSV_HEADER + word("首行标题")).encode())

    assert recognize_first_line(path, runner=fake_runner) == "首行标题"
    assert path.read_bytes() == original


def test_slow_ocr_does_not_block_preview_more_pages_or_manual_name(tmp_path):
    store = store_at(tmp_path)
    started, release = threading.Event(), threading.Event()

    def slow_ocr(path):
        assert path.name == "p1.jpg"
        started.set()
        assert release.wait(3)
        return "迟到识别结果"

    worker = OCRNamingWorker(store, recognizer=slow_ocr)
    store.on_page_published = worker.submit
    worker.start()
    sid = store.create(auto_name=True)["id"]
    original_name = store.get(sid)["name"]
    batch = store.start_batch(sid)
    try:
        store.add_page(sid, batch["id"], 1, jpeg())
        assert started.wait(2)
        assert store.get(sid)["pages"] == [1]
        assert store.get(sid)["name"] == original_name
        store.add_page(sid, batch["id"], 2, jpeg("blue"))
        store.finish_batch(sid, batch["id"])
        assert store.get(sid)["pages"] == [1, 2]
        # Lock happens on the first input event, before the final text is saved.
        assert store.lock_name(sid)["name_source"] == "manual"
        store.rename(sid, "用户正在编辑的名字")
        release.set()
        worker.thread.join(timeout=0.05)
        assert store.get(sid)["name"] == "用户正在编辑的名字"
        assert store.get(sid)["auto_name"] is False
        assert store.finish_ocr(sid, "又一次迟到结果")["name"] == "用户正在编辑的名字"
    finally:
        release.set()
        worker.close()


@pytest.mark.parametrize("outcome", ["", subprocess.TimeoutExpired("tesseract", 10), FileNotFoundError("tesseract")])
def test_ocr_failure_retains_date_name_and_is_not_retried_on_continue(tmp_path, outcome):
    store = store_at(tmp_path)
    calls = []

    def recognize(path):
        calls.append(path)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    worker = OCRNamingWorker(store, recognizer=recognize)
    store.on_page_published = worker.submit
    worker.start()
    try:
        sid = first_page(store)
        document = await_state(store, sid, "failed")
        default = document["name"]
        assert default.startswith("扫描文件 20") and document["name_source"] == "default"
        batch = store.start_batch(sid)
        store.add_page(sid, batch["id"], 1, jpeg())
        store.finish_batch(sid, batch["id"])
        assert store.get(sid)["name"] == default
        assert len(calls) == 1
    finally:
        worker.close()


def test_ocr_and_manual_names_are_atomically_unique_across_all_history(tmp_path):
    store = store_at(tmp_path)
    store.create("同名报告")
    first, second = first_page(store), first_page(store)
    store.begin_ocr(first)
    store.begin_ocr(second)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(store.finish_ocr, sid, "同名报告") for sid in (first, second)]
        futures += [pool.submit(store.create, "同名报告") for _ in range(2)]
        for future in futures:
            future.result(timeout=3)
    assert {document["name"] for document in store.list()} == {
        "同名报告", "同名报告-001", "同名报告-002", "同名报告-003", "同名报告-004"}
    store.rename(first, "最终稿")
    assert store.rename(second, "最终稿")["name"] == "最终稿-001"


def test_restart_recovers_pending_ocr_but_old_metadata_is_never_renamed(tmp_path):
    store = store_at(tmp_path)
    pending = first_page(store)
    store.begin_ocr(pending)  # Persist an interrupted running worker.
    old = first_page(store, name="旧历史名字", auto_name=False)
    path = store.directory(old) / "document.json"
    metadata = json.loads(path.read_text())
    for field in ("name_source", "auto_name", "ocr_state"):
        del metadata[field]
    path.write_text(json.dumps(metadata))

    recovered = store_at(tmp_path)
    assert recovered.get(old)["name_source"] == "manual"
    assert recovered.pending_ocr() == [pending]
    worker = OCRNamingWorker(recovered, recognizer=lambda path: "恢复后的首行标题")
    worker.start()
    try:
        worker.recover()
        assert await_state(recovered, pending, "done")["name"] == "恢复后的首行标题"
        assert recovered.get(old)["name"] == "旧历史名字"
    finally:
        worker.close()


def test_only_first_document_page_is_ocr_and_continuation_never_renames(tmp_path):
    store = store_at(tmp_path)
    calls = []

    def recognize(path):
        calls.append(path)
        return "第一页标题"

    worker = OCRNamingWorker(store, recognizer=recognize)
    store.on_page_published = worker.submit
    worker.start()
    try:
        sid = first_page(store)
        await_state(store, sid, "done")
        batch = store.start_batch(sid)
        store.add_page(sid, batch["id"], 1, jpeg())
        store.finish_batch(sid, batch["id"])
        assert store.get(sid)["name"] == "第一页标题"
        assert len(calls) == 1
    finally:
        worker.close()


def test_create_response_contains_date_name_even_if_worker_finishes_immediately(tmp_path):
    store = store_at(tmp_path)

    def instant_fake_scan(target, args):
        sid, batch_id = args
        store.add_page(sid, batch_id, 1, jpeg())
        store.begin_ocr(sid)
        store.finish_ocr(sid, "已经识别成功")
        store.finish_batch(sid, batch_id)

    scanner = ScanCoordinator(store, worker_launcher=instant_fake_scan)
    response = scanner.start(auto_name=True)
    assert response["name"].startswith("扫描文件 20")
    assert response["name_source"] == "default"
    assert store.get(response["id"])["name"] == "已经识别成功"


def test_deleting_first_page_cancels_pending_or_late_ocr(tmp_path):
    store = store_at(tmp_path)
    sid = first_page(store)
    original_name = store.get(sid)["name"]
    store.begin_ocr(sid)
    store.delete_page(sid, 1)
    assert store.get(sid)["ocr_state"] == "cancelled"
    assert store.finish_ocr(sid, "已删除页面标题")["name"] == original_name
