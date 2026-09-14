import http.client
import io
import json
import threading
from urllib.parse import quote

import pytest
from PIL import Image, ImageDraw

from documents import DocumentStore
from server import ScanCoordinator, ScanHTTPServer


class FakeMonitor:
    def __init__(self):
        self.status = {"state": "online", "message": "测试设备在线", "checked_at": "2026-09-13T00:00:00Z",
                       "supported_dpi": [150, 200, 300], "duplex_supported": True}

    def snapshot(self):
        return dict(self.status)

    def close(self):
        pass


@pytest.fixture
def api(tmp_path):
    def network_forbidden():
        raise AssertionError("Real SMB is forbidden in API tests")

    store = DocumentStore(tmp_path / "documents", processor=lambda data: (data, {}))
    scanner = ScanCoordinator(store, connection_factory=network_forbidden,
                              worker_launcher=lambda *args: None)
    monitor = FakeMonitor()
    server = ScanHTTPServer(("127.0.0.1", 0), store, coordinator=scanner, monitor=monitor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, body=None, raw=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        try:
            payload = raw if raw is not None else (json.dumps(body) if body is not None else None)
            connection.request(method, path, payload, {"Content-Type": "application/json"})
            response = connection.getresponse()
            data = response.read()
            if response.getheader("Content-Type", "").startswith("application/json"):
                data = json.loads(data)
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    yield request, store, scanner, monitor
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def page_data():
    buffer = io.BytesIO()
    Image.new("RGB", (100, 150), "white").save(buffer, "JPEG")
    return buffer.getvalue()


def test_document_create_append_new_name_unicode_download(api):
    request, store, scanner, _ = api
    status, _, first = request("POST", "/scan", {"name": "示例文件初稿", "dpi": 200, "duplex": True})
    assert status == 202
    sid = first["id"]
    assert first["state"] == "scanning" and first["duplex"] is True
    assert request("POST", "/scan", {})[0] == 409
    batch = store.pending_batches()[0][1]["id"]
    store.add_page(sid, batch, 1, page_data())
    assert request("GET", f"/scan/{sid}/status")[2]["pages"] == [1]
    assert request("GET", f"/scan/{sid}/original/1")[2] == page_data()
    store.finish_batch(sid, batch)
    status, _, continued = request("POST", f"/scan/{sid}/continue", {"dpi": 300})
    assert status == 202 and continued["id"] == sid and continued["pages"] == [1]
    batch = store.pending_batches()[0][1]["id"]
    store.add_page(sid, batch, 1, page_data())
    store.finish_batch(sid, batch)
    renamed = request("POST", f"/scan/{sid}/rename", {"name": "示例文件最终稿"})
    assert renamed[0] == 200
    status, headers, pdf = request("GET", f"/scan/{sid}/pdf")
    assert status == 200 and pdf.startswith(b"%PDF-")
    assert "filename*=UTF-8''" + quote("示例文件最终稿.pdf") in headers["Content-Disposition"]
    second = request("POST", "/scan", {"name": "独立文件"})[2]
    assert second["id"] != sid and second["pages"] == []
    history = request("GET", "/documents")[2]
    assert len(history["documents"]) == 2 and history["active_id"] == second["id"]
    assert request("POST", f"/scan/{sid}/delete/1", {})[2]["pages"] == [2]
    assert request("GET", f"/scan/{sid}/page/1")[0] == 404


@pytest.mark.parametrize("payload", [{"dpi": 600}, {"dpi": "150"}, {"dpi": True},
                                      {"duplex": "true"}, {"name": "\r\nInjected"}, {"name": ""}])
def test_invalid_input_creates_no_batch(api, payload):
    request, store, scanner, _ = api
    assert request("POST", "/scan", payload)[0] == 400
    assert store.list() == [] and scanner.active_id is None


def test_malformed_json_and_unknown_document_are_safe(api):
    request, store, _, _ = api
    assert request("POST", "/scan", raw="{")[0] == 400
    assert request("POST", "/scan", raw="[]")[0] == 400
    assert request("POST", "/scan/missing/continue", {})[0] == 404
    assert request("GET", "/scan/%2e%2e/status")[0] == 404
    assert store.list() == []


def test_options_require_device_capability_for_new_features(api):
    request, store, _, monitor = api
    monitor.status = {"state": "unknown", "supported_dpi": []}
    assert request("POST", "/scan", {"dpi": 300})[0] == 400
    assert request("POST", "/scan", {"duplex": True})[0] == 400
    assert store.list() == []
    assert request("POST", "/scan", {"dpi": 150, "duplex": False})[0] == 202


def test_scanner_status_is_cached_and_shows_scan_activity(api):
    request, _, _, _ = api
    assert request("GET", "/scanner/status")[2]["state"] == "online"
    request("POST", "/scan", {})
    assert request("GET", "/scanner/status")[2]["state"] == "scanning"


def test_auto_name_immediately_returns_default_then_input_lock_beats_late_ocr(api):
    request, store, _, _ = api
    status, _, document = request("POST", "/scan", {"auto_name": True, "name": "旧输入值"})
    assert status == 202
    sid = document["id"]
    assert document["name"].startswith("扫描文件 20") and document["name"] != "旧输入值"
    assert document["auto_name"] is True and document["name_source"] == "default"
    batch = store.pending_batches()[0][1]["id"]
    store.add_page(sid, batch, 1, page_data())
    store.begin_ocr(sid)
    status, _, locked = request("POST", f"/scan/{sid}/name-lock", {})
    assert status == 200 and locked["name_source"] == "manual"
    assert request("POST", f"/scan/{sid}/rename", {"name": ""})[0] == 400
    assert store.finish_ocr(sid, "迟到结果")["name"] == document["name"]
    result = request("POST", f"/scan/{sid}/rename", {"name": "人工最终名字"})
    assert result[2]["name"] == "人工最终名字" and result[2]["auto_name"] is False


def test_manual_create_disables_ocr_and_invalid_auto_flag_is_rejected(api):
    request, store, _, _ = api
    assert request("POST", "/scan", {"auto_name": "true"})[0] == 400
    assert request("POST", "/scan", {"auto_name": None})[0] == 400
    document = request("POST", "/scan", {"auto_name": False, "name": "指定名称"})[2]
    assert document["name"] == "指定名称" and document["name_source"] == "manual"
    assert document["ocr_state"] == "skipped"
    assert request("POST", "/scan/missing/name-lock", {})[0] == 404


def test_reorder_and_rescan_capability_revision_and_concurrency(api):
    request, store, scanner, monitor = api
    sid = request("POST", "/scan", {"name": "不变名称", "dpi": 300})[2]["id"]
    batch = store.pending_batches()[0][1]["id"]
    store.add_page(sid, batch, 1, page_data())
    store.add_page(sid, batch, 2, page_data())
    revision = store.get(sid)["order_revision"]
    assert request("POST", f"/scan/{sid}/reorder", {"pages": [2, 1], "order_revision": revision})[0] == 409
    store.finish_batch(sid, batch)
    status, _, document = request("POST", f"/scan/{sid}/reorder", {"pages": [2, 1], "order_revision": revision})
    assert status == 200 and document["pages"] == [2, 1]
    assert request("POST", f"/scan/{sid}/reorder", {"pages": [1, 2], "order_revision": revision})[0] == 409
    assert request("POST", f"/scan/{sid}/reorder", {"pages": [1], "order_revision": document["order_revision"]})[0] == 400
    assert request("POST", f"/scan/{sid}/rescan/1", {})[0] == 400
    monitor.status["supports_page_rescan"] = True
    status, _, replacement = request("POST", f"/scan/{sid}/rescan/1", {})
    assert status == 202 and replacement["rescan_page"] == 1 and replacement["pages"] == [2, 1]
    assert replacement["name"] == "不变名称"
    batch = store.pending_batches()[0][1]
    assert batch["max_pages"] == 1 and batch["dpi"] == 300 and batch["duplex"] is False
    assert request("POST", f"/scan/{sid}/rescan/2", {})[0] == 409
    assert request("POST", "/scan", {})[0] == 409
    assert request("POST", f"/scan/{sid}/delete/1", {})[0] == 409
    store.add_page(sid, batch["id"], 1, page_data())
    store.finish_batch(sid, batch["id"])
    document = request("GET", f"/scan/{sid}/status")[2]
    assert document["rescan_page"] is None and document["page_details"]["1"]["revision"] == 2
    assert document["pages"] == [2, 1]
    assert request("GET", f"/scan/{sid}/page/1?v=2")[0] == 200


def ready_api_document(store):
    sid = store.create("手动文件名")["id"]
    store.start_batch(sid, "fixture-pages")
    for n in (1, 2, 3):
        store.add_page(sid, "fixture-pages", n, page_data())
    store.finish_batch(sid, "fixture-pages")
    return sid


def test_delete_document_api_hides_downloads_and_edits_while_another_file_scans(api):
    request, store, scanner, _ = api
    sid = ready_api_document(store)
    another = store.create("另一份文件")["id"]
    store.start_batch(another, "pending-another")
    snapshot = store.get(sid)
    status, _, deleted = request("POST", f"/scan/{sid}/delete-document",
                                 {"updated_at": snapshot["updated_at"]})
    assert status == 200 and deleted == {"ok": True, "id": sid}
    history = request("GET", "/documents")[2]
    assert [d["id"] for d in history["documents"]] == [another]
    assert history["active_id"] == another and scanner.active_id == another
    for path in ("status", "page/1", "original/1", "pdf"):
        assert request("GET", f"/scan/{sid}/{path}")[0] == 404
    for path, payload in (("rename", {"name": "不能复活"}), ("name-lock", {}),
                          ("delete/1", {}), ("blank-analysis", {}),
                          ("delete-document", {"updated_at": snapshot["updated_at"]})):
        assert request("POST", f"/scan/{sid}/{path}", payload)[0] == 404
    store.finish_batch(another, "pending-another")
    assert request("POST", f"/scan/{sid}/continue", {})[0] == 404
    assert request("POST", f"/scan/{sid}/rescan/1", {})[0] == 404


def test_delete_document_api_rejects_invalid_stale_and_active_requests(api):
    request, store, _, _ = api
    sid = ready_api_document(store)
    snapshot = store.get(sid)
    for payload in ({}, {"updated_at": None}, {"updated_at": True}, {"updated_at": ""},
                    {"updated_at": []}, {"updated_at": snapshot["updated_at"], "extra": True}):
        assert request("POST", f"/scan/{sid}/delete-document", payload)[0] == 400
    store.rename(sid, "刚更新的文件名")
    assert request("POST", f"/scan/{sid}/delete-document", {"updated_at": snapshot["updated_at"]})[0] == 409
    store.start_batch(sid, "pending-delete")
    assert request("POST", f"/scan/{sid}/delete-document",
                   {"updated_at": store.get(sid)["updated_at"]})[0] == 409
    assert request("POST", "/scan/missing/delete-document", {"updated_at": snapshot["updated_at"]})[0] == 404
    assert store.get(sid)["pages"] == snapshot["pages"]


def test_delete_document_api_save_failure_does_not_hide_document(api, monkeypatch):
    request, store, _, _ = api
    sid = ready_api_document(store)
    snapshot = store.get(sid)

    def failed_write(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr("documents.atomic_write", failed_write)
    assert request("POST", f"/scan/{sid}/delete-document", {"updated_at": snapshot["updated_at"]})[0] == 500
    assert request("GET", f"/scan/{sid}/status")[2] == snapshot


def test_render_mode_creation_validation_and_continuation_preserve_preference(api):
    request, store, scanner, _ = api
    for mode in (None, True, 1, [], {}, "", "unknown"):
        assert request("POST", "/scan", {"render_mode": mode})[0] == 400
        assert store.list() == [] and scanner.active_id is None
    status, _, document = request("POST", "/scan", {"render_mode": "bw"})
    assert status == 202 and document["render_mode"] == "bw"
    sid = document["id"]
    store.finish_batch(sid)
    continued = request("POST", f"/scan/{sid}/continue", {})
    assert continued[0] == 202 and continued[2]["render_mode"] == "bw"
    # This preference is independent of the physical scan pipeline.
    snapshot = store.get(sid)
    changed = request("POST", f"/scan/{sid}/set-render-mode",
                      {"render_mode": "enhanced", "updated_at": snapshot["updated_at"]})
    assert changed[0] == 200 and changed[2]["render_mode"] == "enhanced"
    assert changed[2]["active_batch"] == snapshot["active_batch"]


def test_render_mode_api_validates_schema_snapshot_and_deleted_file(api):
    request, store, _, _ = api
    sid = ready_api_document(store)
    before = store.get(sid)
    assert before["render_mode"] == "original" and isinstance(before["render_version"], int)
    payload = {"render_mode": "enhanced", "updated_at": before["updated_at"]}
    invalid = ({}, {"render_mode": "bw"}, dict(payload, extra=True),
               dict(payload, render_mode=True), dict(payload, render_mode="invalid"),
               dict(payload, updated_at=None), dict(payload, updated_at=""))
    for body in invalid:
        assert request("POST", f"/scan/{sid}/set-render-mode", body)[0] == 400
    changed = request("POST", f"/scan/{sid}/set-render-mode", payload)
    assert changed[0] == 200 and changed[2]["render_mode"] == "enhanced"
    assert request("POST", f"/scan/{sid}/set-render-mode", dict(payload, render_mode="bw"))[0] == 409
    store.delete_document(sid, updated_at=store.get(sid)["updated_at"])
    assert request("POST", f"/scan/{sid}/set-render-mode", payload)[0] == 404
    assert request("GET", f"/scan/{sid}/page/1?mode=enhanced")[0] == 404


def test_rendered_preview_and_pdf_match_and_original_route_keeps_raw_bytes(api):
    request, store, _, _ = api
    sid = ready_api_document(store)
    output = io.BytesIO()
    Image.new("L", (100, 150), 80).save(output, "JPEG")
    rendered = output.getvalue()
    calls = []

    def renderer(data, mode):
        calls.append(mode)
        assert data == page_data() and mode == "bw"
        return rendered

    store.enhancer = renderer
    snapshot = store.get(sid)
    query = f"?mode=bw&v=1&ev={snapshot['render_version']}"
    result = request("GET", f"/scan/{sid}/page/1{query}")
    assert result[0] == 200 and result[2] == rendered and calls == ["bw"]
    assert request("GET", f"/scan/{sid}/original/1{query}")[2] == page_data()
    assert request("GET", f"/scan/{sid}/page/1?mode=original")[2] == page_data()
    request("POST", f"/scan/{sid}/set-render-mode", {"render_mode": "bw", "updated_at": snapshot["updated_at"]})
    pdf = request("GET", f"/scan/{sid}/pdf")
    assert pdf[0] == 200 and rendered in pdf[2]
    assert pdf[2].count(b"/ColorSpace /DeviceGray") == 3 and calls == ["bw"] * 3
    for query in ("?mode=nope", "?mode=", "?mode=bw&mode=enhanced", "?v=0", "?v=x", "?ev=x"):
        assert request("GET", f"/scan/{sid}/page/1{query}")[0] == 400
    for query in ("?v=2", "?ev=99999"):
        assert request("GET", f"/scan/{sid}/page/1{query}")[0] == 409


def page_selection(document, pages):
    return {"pages": pages, "order_revision": document["order_revision"],
            "page_revisions": {str(n): document["page_details"][str(n)]["revision"]
                               for n in document["pages"]}}


def test_blank_marking_manual_selection_and_persistent_undo_api_work_offline(api):
    request, store, scanner, monitor = api
    sid = ready_api_document(store)
    monitor.status = {"state": "offline", "supported_dpi": [], "duplex_supported": False}
    store.blank_analyzer = lambda data: {"is_blank": True}
    status, _, result = request("POST", f"/scan/{sid}/blank-analysis", {})
    assert status == 200 and result["examined"] == 3
    assert [p["page"] for p in result["candidates"]] == [1, 2, 3]
    assert result["page_revisions"] == {"1": 1, "2": 1, "3": 1}
    assert request("GET", f"/scan/{sid}/status")[2]["pages"] == [1, 2, 3]
    status, _, deleted = request("POST", f"/scan/{sid}/delete-selected",
                                {"pages": [2], "order_revision": result["order_revision"],
                                 "page_revisions": result["page_revisions"]})
    assert status == 200 and deleted["document"]["pages"] == [1, 3] and deleted["removed"] == [2]
    assert deleted["document"]["blank_undo"] == {"cleanup_id": deleted["cleanup_id"], "count": 1}
    assert request("GET", f"/scan/{sid}/page/2")[0] == 404
    assert request("POST", f"/scan/{sid}/rename", {"name": "删除后编辑名称"})[0] == 200
    status, _, restored = request("POST", f"/scan/{sid}/blank-undo", {"cleanup_id": deleted["cleanup_id"]})
    assert status == 200 and restored["restored"] == [2]
    assert restored["document"]["name"] == "删除后编辑名称"
    assert restored["document"]["pages"] == [1, 2, 3] and restored["document"]["blank_undo"] is None
    assert request("GET", f"/scan/{sid}/original/2")[2] == page_data()
    assert request("POST", f"/scan/{sid}/blank-undo", {"cleanup_id": deleted["cleanup_id"]})[0] == 409
    # Pure manual selection never depends on an analysis or scanner capability.
    store.blank_analyzer = lambda data: pytest.fail("Manual deletion must not classify images")
    payload = page_selection(restored["document"], [1, 3])
    assert request("POST", f"/scan/{sid}/delete-selected", payload)[2]["document"]["pages"] == [2]
    assert scanner.active_id is None


def test_blank_operations_report_stale_busy_and_bad_schema_without_deleting(api):
    request, store, _, _ = api
    sid = ready_api_document(store)
    original = store.get(sid)
    for action in ("blank-analysis", "delete-selected", "blank-undo"):
        assert request("POST", f"/scan/{sid}/{action}", {"unexpected": True})[0] == 400
    payload = page_selection(original, [1])
    for key, value in (("pages", [True]), ("order_revision", True), ("page_revisions", {"1": "1"})):
        assert request("POST", f"/scan/{sid}/delete-selected", dict(payload, **{key: value}))[0] == 400
    assert request("POST", "/scan/missing/blank-analysis", {})[0] == 404
    assert request("POST", f"/scan/{sid}/blank-undo", {"cleanup_id": "0" * 32})[0] == 409
    store.reorder(sid, [3, 1, 2], original["order_revision"])
    assert request("POST", f"/scan/{sid}/delete-selected", payload)[0] == 409
    result = request("POST", f"/scan/{sid}/delete-selected", page_selection(store.get(sid), [2]))[2]
    store.start_batch(sid, "busy")
    store.blank_analyzer = lambda data: pytest.fail("Scanner activity blocks analysis")
    assert request("POST", f"/scan/{sid}/blank-analysis", {})[0] == 409
    assert request("POST", f"/scan/{sid}/delete-selected", page_selection(store.get(sid), [1]))[0] == 409
    assert request("POST", f"/scan/{sid}/blank-undo", {"cleanup_id": result["cleanup_id"]})[0] == 409
    assert store.get(sid)["pages"] == [3, 1]


def test_analysis_changed_snapshot_is_conflict_and_partial_failure_is_retained(api):
    request, store, _, _ = api
    sid = ready_api_document(store)

    def reordered(data):
        document = store.get(sid)
        store.reorder(sid, [3, 2, 1], document["order_revision"])
        return {"is_blank": True}

    store.blank_analyzer = reordered
    assert request("POST", f"/scan/{sid}/blank-analysis", {})[0] == 409

    def failed(data):
        raise ValueError("fixture failure")

    store.blank_analyzer = failed
    status, _, result = request("POST", f"/scan/{sid}/blank-analysis", {})
    assert status == 200 and result["candidates"] == [] and len(result["errors"]) == 3
    assert store.get(sid)["pages"] == [3, 2, 1]


def test_bulk_delete_api_failed_commit_keeps_original_pages(api, monkeypatch):
    request, store, _, _ = api
    sid = ready_api_document(store)
    before = store.get(sid)

    def failed(document):
        raise OSError("fixture disk failure")

    monkeypatch.setattr(store, "_save", failed)
    status, _, result = request("POST", f"/scan/{sid}/delete-selected", page_selection(before, [1, 2]))
    assert status == 500 and "error" in result
    assert request("GET", f"/scan/{sid}/status")[2] == before


def test_real_blank_classifier_http_mark_select_delete_undo_and_pdf_order(api):
    """Image-only integration: real classifier, fake store inputs, no scanner."""
    fitz = pytest.importorskip("fitz")
    request, store, scanner, monitor = api
    monitor.status = {"state": "offline", "supported_dpi": []}
    sid = store.create("保留文档名")["id"]
    originals = {}
    store.start_batch(sid, "synthetic-pages", duplex=True)
    for number, color in [(1, "red"), (2, None), (3, "blue"), (4, None)]:
        image = Image.new("RGB", (600, 900), "white")
        if color:
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, 50, 50), fill=color)
            draw.text((100, 100), "Document content 2026", fill="black")
            draw.line((100, 160, 450, 160), fill="black", width=2)
        output = io.BytesIO()
        image.save(output, "JPEG", quality=95)
        originals[number] = output.getvalue()
        store.add_page(sid, "synthetic-pages", number, originals[number])
    store.finish_batch(sid, "synthetic-pages")
    store.reorder(sid, [3, 1, 2, 4], store.get(sid)["order_revision"])
    before = store.get(sid)

    status, _, analysis = request("POST", f"/scan/{sid}/blank-analysis", {})
    assert status == 200 and analysis["errors"] == [] and analysis["examined"] == 4
    assert analysis["candidates"] == [{"page": 2, "position": 3, "revision": 1},
                                      {"page": 4, "position": 4, "revision": 1}]
    assert store.get(sid) == before
    # The user unchecks blank page 2 and explicitly adds content page 1.
    status, _, deleted = request("POST", f"/scan/{sid}/delete-selected", {
        "pages": [1, 4], "order_revision": analysis["order_revision"],
        "page_revisions": analysis["page_revisions"]})
    assert status == 200 and deleted["document"]["pages"] == [3, 2]
    with fitz.open(stream=request("GET", f"/scan/{sid}/pdf")[2], filetype="pdf") as pdf:
        assert len(pdf) == 2 and pdf[0].get_pixmap().samples[2] > 240
        assert all(value > 240 for value in pdf[1].get_pixmap().samples[:3])
    status, _, undone = request("POST", f"/scan/{sid}/blank-undo", {"cleanup_id": deleted["cleanup_id"]})
    assert status == 200 and undone["document"]["pages"] == [3, 1, 2, 4]
    assert undone["document"]["name"] == before["name"]
    with fitz.open(stream=request("GET", f"/scan/{sid}/pdf")[2], filetype="pdf") as pdf:
        assert len(pdf) == 4 and pdf[0].get_pixmap().samples[2] > 240
        assert pdf[1].get_pixmap().samples[0] > 240
    for number, data in originals.items():
        assert request("GET", f"/scan/{sid}/original/{number}")[2] == data
    assert scanner.active_id is None
