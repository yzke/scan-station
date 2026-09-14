from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import threading

import pytest
from PIL import Image, ImageOps

import documents
from documents import DocumentConflict, DocumentStore


def jpeg(color="white", size=(80, 120), mode="RGB"):
    output = io.BytesIO()
    Image.new(mode, size, color).save(output, "JPEG")
    return output.getvalue()


class FakeEnhancer:
    def __init__(self):
        self.calls = []

    def __call__(self, data, mode):
        self.calls.append((data, mode))
        with Image.open(io.BytesIO(data)) as source:
            result = source.convert("L") if mode == "bw" else ImageOps.invert(source.convert("RGB"))
            output = io.BytesIO()
            result.save(output, "JPEG")
            return output.getvalue()


def ready_document(store, colors=("red",), name="渲染文件"):
    sid = store.create(name)["id"]
    batch = store.start_batch(sid, dpi=300)
    for n, color in enumerate(colors, 1):
        store.add_page(sid, batch["id"], n, jpeg(color))
    store.finish_batch(sid, batch["id"])
    return sid


def render_store(tmp_path, enhancer=None, colors=("red",)):
    enhancer = enhancer or FakeEnhancer()
    store = DocumentStore(tmp_path, processor=lambda data: (data, {}), enhancer=enhancer)
    return store, ready_document(store, colors), enhancer


def test_render_mode_default_cas_persistence_and_failed_save(tmp_path, monkeypatch):
    store, sid, renderer = render_store(tmp_path)
    before = store.get(sid)
    assert before["render_mode"] == "original"
    assert before["render_version"] == documents.ENHANCEMENT_VERSION
    original = store.page_bytes(sid, 1)
    selected = store.set_render_mode(sid, "enhanced", updated_at=before["updated_at"])
    assert selected["render_mode"] == "enhanced" and selected["updated_at"] != before["updated_at"]
    assert renderer.calls == []
    for key in ("pages", "page_details", "order_revision", "name", "name_source", "ocr_state"):
        assert selected[key] == before[key]
    with pytest.raises(DocumentConflict):
        store.set_render_mode(sid, "bw", updated_at=before["updated_at"])
    assert store.set_render_mode(sid, "enhanced", updated_at=selected["updated_at"]) == selected
    restored = DocumentStore(tmp_path, enhancer=renderer)
    assert restored.get(sid)["render_mode"] == "enhanced"
    metadata_path = store.directory(sid) / "document.json"
    saved = metadata_path.read_bytes()

    def failed_write(*args, **kwargs):
        raise OSError("simulated metadata failure")

    monkeypatch.setattr(documents, "atomic_write", failed_write)
    with pytest.raises(OSError):
        store.set_render_mode(sid, "bw", updated_at=selected["updated_at"])
    assert store.get(sid) == selected and metadata_path.read_bytes() == saved
    assert store.page_bytes(sid, 1) == original == store.page_bytes(sid, 1, original=True)


def test_legacy_render_mode_default_does_not_rewrite_metadata(tmp_path):
    store, sid, _ = render_store(tmp_path)
    path = store.directory(sid) / "document.json"
    metadata = json.loads(path.read_text())
    metadata.pop("render_mode")
    path.write_text(json.dumps(metadata))
    saved = path.read_bytes()
    restored = DocumentStore(tmp_path)
    assert restored.get(sid)["render_mode"] == "original"
    assert path.read_bytes() == saved


def test_render_mode_keeps_ocr_and_blank_analysis_on_corrected_base(tmp_path):
    renderer = FakeEnhancer()
    store = DocumentStore(tmp_path, processor=lambda data: (data, {}), enhancer=renderer)
    sid = store.create(auto_name=True, render_mode="bw")["id"]
    batch = store.start_batch(sid)
    original = jpeg("red")
    store.add_page(sid, batch["id"], 1, original)
    store.finish_batch(sid, batch["id"])
    ocr_path = store.begin_ocr(sid)
    assert ocr_path.read_bytes() == original and renderer.calls == []
    analyzed = []

    def classify(data):
        analyzed.append(data)
        return {"is_blank": False}

    store.blank_analyzer = classify
    assert store.analyze_blank_pages(sid)["examined"] == 1
    assert analyzed == [original] and renderer.calls == []
    assert store.rendered_page_bytes(sid, 1) != original
    assert ocr_path.read_bytes() == original
    assert store.get(sid)["ocr_state"] == "running"


def test_lazy_render_cache_is_shared_by_preview_pdf_and_restart(tmp_path):
    store, sid, renderer = render_store(tmp_path)
    original = store.page_bytes(sid, 1)
    store.set_render_mode(sid, "enhanced", updated_at=store.get(sid)["updated_at"])
    metadata = (store.directory(sid) / "document.json").read_bytes()
    rendered = store.rendered_page_bytes(sid, 1, mode="enhanced", revision=1,
                                         render_version=documents.ENHANCEMENT_VERSION)
    assert rendered != original and len(renderer.calls) == 1
    assert store.rendered_page_bytes(sid, 1) == rendered
    pdf, name = store.pdf(sid)
    assert rendered in pdf and name == "渲染文件" and len(renderer.calls) == 1
    assert (store.directory(sid) / "document.json").read_bytes() == metadata
    restored = DocumentStore(tmp_path, enhancer=lambda *args: pytest.fail("Cache was not reused"))
    assert restored.rendered_page_bytes(sid, 1) == rendered
    assert restored.page_bytes(sid, 1) == original == restored.page_bytes(sid, 1, original=True)


def test_render_cache_recovers_missing_corrupt_and_wrong_size_files(tmp_path):
    store, sid, renderer = render_store(tmp_path)
    expected = store.rendered_page_bytes(sid, 1, mode="enhanced")
    cache = next((store.directory(sid) / "render-cache").rglob("*.jpg"))
    for bad in (None, b"broken JPEG", jpeg(size=(20, 30))):
        if bad is None:
            cache.unlink()
        else:
            cache.write_bytes(bad)
        previous_calls = len(renderer.calls)
        assert store.rendered_page_bytes(sid, 1, mode="enhanced") == expected
        assert len(renderer.calls) == previous_calls + 1
        assert cache.read_bytes() == expected


def test_render_cache_tracks_mode_source_revision_and_algorithm_version(tmp_path, monkeypatch):
    store, sid, renderer = render_store(tmp_path)
    first = store.rendered_page_bytes(sid, 1, mode="enhanced")
    black_white = store.rendered_page_bytes(sid, 1, mode="bw")
    assert first != black_white and len(renderer.calls) == 2
    batch = store.start_batch(sid, target_page=1)
    store.add_page(sid, batch["id"], 1, jpeg("blue"))
    store.finish_batch(sid, batch["id"])
    with pytest.raises(DocumentConflict):
        store.rendered_page_bytes(sid, 1, mode="enhanced", revision=1)
    replacement = store.rendered_page_bytes(sid, 1, mode="enhanced", revision=2)
    assert replacement != first and len(renderer.calls) == 3
    version = documents.ENHANCEMENT_VERSION
    monkeypatch.setattr(documents, "ENHANCEMENT_VERSION", version + 1)
    with pytest.raises(DocumentConflict):
        store.rendered_page_bytes(sid, 1, mode="enhanced", render_version=version)
    assert store.rendered_page_bytes(sid, 1, mode="enhanced", render_version=version + 1) == replacement
    assert len(renderer.calls) == 4


@pytest.mark.parametrize("output", [b"broken JPEG", jpeg()[:-2], jpeg(size=(20, 30))])
def test_renderer_rejects_invalid_output_and_retains_original(tmp_path, output):
    store, sid, _ = render_store(tmp_path, enhancer=lambda *args: output)
    before = store.get(sid)
    original = store.page_bytes(sid, 1)
    with pytest.raises(ValueError):
        store.rendered_page_bytes(sid, 1, mode="enhanced")
    assert store.rendered_page_bytes(sid, 1, mode="original") == original
    assert store.get(sid) == before
    assert not list((store.directory(sid) / "render-cache").rglob("*.jpg"))


def test_render_cache_write_failure_is_recoverable_without_changing_document(tmp_path, monkeypatch):
    store, sid, renderer = render_store(tmp_path)
    metadata_path = store.directory(sid) / "document.json"
    before = metadata_path.read_bytes()
    writer = documents.atomic_write

    def failed_cache_write(path, data):
        if "render-cache" in Path(path).parts:
            raise OSError("simulated cache failure")
        return writer(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(documents, "atomic_write", failed_cache_write)
        rendered = store.rendered_page_bytes(sid, 1, mode="enhanced")
        assert store.rendered_page_bytes(sid, 1, mode="enhanced") == rendered
    assert len(renderer.calls) == 2
    assert store.rendered_page_bytes(sid, 1, mode="enhanced") == rendered
    assert store.rendered_page_bytes(sid, 1, mode="enhanced") == rendered
    assert len(renderer.calls) == 3 and metadata_path.read_bytes() == before


def test_slow_renderer_releases_store_lock_and_discards_deleted_file(tmp_path):
    started, release = threading.Event(), threading.Event()
    renderer = FakeEnhancer()

    def slow(data, mode):
        started.set()
        assert release.wait(3)
        return renderer(data, mode)

    store, sid, _ = render_store(tmp_path, enhancer=slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rendering = pool.submit(store.rendered_page_bytes, sid, 1, mode="enhanced")
        try:
            assert started.wait(2)
            renamed = pool.submit(store.rename, sid, "可同时改名").result(timeout=1)
            pool.submit(store.delete_document, sid, updated_at=renamed["updated_at"]).result(timeout=1)
        finally:
            release.set()
        with pytest.raises(KeyError):
            rendering.result(timeout=2)
    assert store.list() == []
    assert not list((store.directory(sid) / "render-cache").rglob("*.jpg"))


def test_render_concurrency_is_bounded_and_queued_deleted_file_is_skipped(tmp_path):
    started, release, second_started = threading.Event(), threading.Event(), threading.Event()
    renderer = FakeEnhancer()

    def slow(data, mode):
        if started.is_set():
            second_started.set()
        started.set()
        assert release.wait(3)
        return renderer(data, mode)

    store, first, _ = render_store(tmp_path, enhancer=slow)
    second = ready_document(store, name="排队中的渲染")
    with ThreadPoolExecutor(max_workers=3) as pool:
        one = pool.submit(store.rendered_page_bytes, first, 1, mode="enhanced")
        assert started.wait(2)
        two = pool.submit(store.rendered_page_bytes, second, 1, mode="enhanced")
        try:
            assert not second_started.wait(.1)
            pool.submit(store.delete_document, second,
                        updated_at=store.get(second)["updated_at"]).result(timeout=1)
        finally:
            release.set()
        assert one.result(timeout=2)
        with pytest.raises(KeyError):
            two.result(timeout=2)
    assert len(renderer.calls) == 1


def test_pdf_pins_mode_order_name_and_allows_subsequent_append(tmp_path):
    started, release = threading.Event(), threading.Event()
    renderer = FakeEnhancer()

    def slow(data, mode):
        if not started.is_set():
            started.set()
            assert release.wait(3)
        return renderer(data, mode)

    store, sid, _ = render_store(tmp_path, enhancer=slow, colors=("red", "blue"))
    original_pages = [store.page_bytes(sid, n) for n in (1, 2)]
    store.set_render_mode(sid, "enhanced", updated_at=store.get(sid)["updated_at"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        rendering = pool.submit(store.pdf, sid)
        try:
            assert started.wait(2)
            renamed = pool.submit(store.rename, sid, "后来改名").result(timeout=1)
            store.set_render_mode(sid, "bw", updated_at=renamed["updated_at"])
            store.reorder(sid, [2, 1], store.get(sid)["order_revision"])
            batch = store.start_batch(sid)
            store.add_page(sid, batch["id"], 1, jpeg("green"))
            store.finish_batch(sid, batch["id"])
        finally:
            release.set()
        pdf, name = rendering.result(timeout=2)
    assert name == "渲染文件" and renderer.calls == [(data, "enhanced") for data in original_pages]
    expected = [FakeEnhancer()(data, "enhanced") for data in original_pages]
    assert pdf.index(expected[0]) < pdf.index(expected[1]) and b"/Count 2" in pdf
    latest_pdf, latest_name = store.pdf(sid)
    assert latest_name == "后来改名" and b"/Count 3" in latest_pdf
    assert latest_pdf.count(b"/ColorSpace /DeviceGray") == 3
    assert store.rendered_page_bytes(sid, 1, mode="bw") in latest_pdf


@pytest.mark.parametrize("change", ["delete_document", "delete_page", "replace_page"])
def test_pdf_rejects_deleted_or_replaced_source_during_render(tmp_path, change):
    started, release = threading.Event(), threading.Event()

    def slow(data, mode):
        started.set()
        assert release.wait(3)
        return FakeEnhancer()(data, mode)

    store, sid, _ = render_store(tmp_path, enhancer=slow)
    store.set_render_mode(sid, "enhanced", updated_at=store.get(sid)["updated_at"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        rendering = pool.submit(store.pdf, sid)
        try:
            assert started.wait(2)
            current = pool.submit(store.get, sid).result(timeout=1)
            if change == "delete_document":
                store.delete_document(sid, updated_at=current["updated_at"])
            elif change == "delete_page":
                store.delete_page(sid, 1)
            else:
                batch = store.start_batch(sid, target_page=1)
                store.add_page(sid, batch["id"], 1, jpeg("blue"))
                store.finish_batch(sid, batch["id"])
        finally:
            release.set()
        with pytest.raises(DocumentConflict if change == "replace_page" else KeyError):
            rendering.result(timeout=2)
