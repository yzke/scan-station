"""Persistent scan documents, with recoverable page publication and PDF export."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import threading
import time
import unicodedata
import uuid

from PIL import Image

from image_enhancement import ENHANCEMENT_MODES, ENHANCEMENT_VERSION, enhance_jpeg
from ocr_naming import clean_filename

ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")
SUPPORTED_DPI = (150, 200, 300)


class DocumentConflict(Exception):
    """A stale page order or an operation conflicting with an active batch."""


def timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_id():
    return time.strftime("s%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:12]


def scan_options(dpi=150, duplex=False):
    if type(dpi) is not int or dpi not in SUPPORTED_DPI:
        raise ValueError("DPI 必须是 150、200 或 300")
    if type(duplex) is not bool:
        raise ValueError("duplex 必须是布尔值")
    return {"dpi": dpi, "duplex": duplex}


def validate_render_mode(mode):
    if not isinstance(mode, str) or mode not in ENHANCEMENT_MODES:
        raise ValueError("图片模式必须是 original、enhanced 或 bw")
    return mode


def document_name(name):
    if not isinstance(name, str):
        raise ValueError("文件名必须是文字")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("文件名不能包含控制字符")
    name = name.strip()
    if not name or len(name) > 120:
        raise ValueError("文件名须为 1–120 个字符，不能包含控制字符")
    return name


def complete_jpeg(data):
    """Pillow normally accepts a missing EOI; require it before publication."""
    if not data.startswith(b"\xff\xd8") or not data.rstrip().endswith(b"\xff\xd9"):
        raise ValueError("JPEG 尚未写入完成")
    with Image.open(io.BytesIO(data)) as im:
        if im.format != "JPEG":
            raise ValueError("页面不是 JPEG")
        im.verify()
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        return im.size


def fsync_directory(path):
    directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def durable_mkdir(path):
    """Persist every new ancestor's directory entry, not only the leaf."""
    path = Path(path)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            if not directory.is_dir():
                raise
        fsync_directory(directory.parent)


def atomic_write(path, data):
    path = Path(path)
    durable_mkdir(path.parent)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def default_processor(data):
    from image_processing import process_page
    return process_page(data)


def default_blank_analyzer(data):
    from blank_pages import analyze_page
    return analyze_page(data)


class DocumentStore:
    def __init__(self, root, *, processor=None, blank_analyzer=None, enhancer=None, legacy_root=None, recover=True):
        self.root = Path(root)
        if recover:
            durable_mkdir(self.root)
        elif not self.root.is_dir():
            raise FileNotFoundError(self.root)
        self.processor = processor or default_processor
        self.blank_analyzer = blank_analyzer or default_blank_analyzer
        self.enhancer = enhancer or enhance_jpeg
        self.lock = threading.RLock()
        # Limit large image working buffers independently of scan collection.
        # Never acquire this lock while holding the document metadata lock.
        self.render_lock = threading.Lock()
        self.on_page_published = None
        self._documents = {}
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or not ID_RE.fullmatch(directory.name):
                continue
            metadata = directory / "document.json"
            if not metadata.exists():
                continue
            # Atomic writes keep this valid. Fail visibly on corruption instead of
            # silently hiding history or freeing a still-active scanner.
            document = json.loads(metadata.read_text(encoding="utf-8"))
            if document.get("id") != directory.name:
                raise ValueError(f"文档标识不匹配: {metadata}")
            self._documents[document["id"]] = document
            if document.get("deleted_at"):
                continue
            naming_changed = self._upgrade_naming(document, recover=recover)
            order_changed = self._upgrade_order(document)
            if recover:
                self._recover_pages(document)
            if recover and (naming_changed or order_changed):
                self._save(document, touch=False)
        if legacy_root is not None and recover:
            self.migrate_legacy(legacy_root)

    def directory(self, document_id):
        if not isinstance(document_id, str) or not ID_RE.fullmatch(document_id):
            raise KeyError(document_id)
        return self.root / document_id

    def _document(self, document_id):
        self.directory(document_id)
        document = self._documents[document_id]
        if document.get("deleted_at"):
            raise KeyError(document_id)
        return document

    def _save(self, document, *, touch=True):
        if touch:
            # Deletion compares this exact public snapshot. Advance even when
            # multiple edits share a clock tick or the system clock moves back.
            previous = datetime.fromisoformat(document["updated_at"].replace("Z", "+00:00"))
            updated = max(datetime.now(timezone.utc), previous + timedelta(microseconds=1))
            document["updated_at"] = updated.isoformat(timespec="microseconds").replace("+00:00", "Z")
        atomic_write(self.directory(document["id"]) / "document.json",
                     json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8"))

    def _public(self, document):
        result = {key: copy.deepcopy(document[key]) for key in
                  ("id", "name", "state", "msg", "created_at", "updated_at", "dpi", "duplex",
                   "name_source", "auto_name", "ocr_state", "order_revision")}
        result["render_mode"] = document.get("render_mode", "original")
        result["render_version"] = ENHANCEMENT_VERSION
        result["pages"] = self._visible_pages(document)
        result["page_details"] = {str(n): copy.deepcopy(document["page_details"][str(n)])
                                  for n in result["pages"]}
        result["active_batch"] = next((b["id"] for b in document["batches"] if b["pending"]), None)
        result["rescan_page"] = next((b.get("target_page") for b in document["batches"] if b["pending"]), None)
        undo = self._blank_undo_record(document)
        result["blank_undo"] = {"cleanup_id": undo["cleanup_id"], "count": len(undo["removed"])} if undo else None
        return result

    @staticmethod
    def _visible_pages(document):
        return [n for n in document["page_order"]
                if document["page_details"][str(n)].get("ready")
                and not document["page_details"][str(n)].get("deleted")]

    def _page_revisions(self, document):
        return {str(n): document["page_details"][str(n)]["revision"] for n in self._visible_pages(document)}

    def _check_page_snapshot(self, document, order_revision, page_revisions):
        if type(order_revision) is not int or order_revision < 0:
            raise ValueError("缺少有效的页序版本")
        if (not isinstance(page_revisions, dict)
                or any(not isinstance(n, str) or not re.fullmatch(r"[1-9][0-9]*", n)
                       or type(revision) is not int or revision < 1
                       for n, revision in page_revisions.items())):
            raise ValueError("缺少有效的完整页面版本")
        if self.pending_batches():
            raise DocumentConflict("扫描期间不能分析或批量删除、恢复页面，请等待当前批次结束")
        if order_revision != document["order_revision"] or page_revisions != self._page_revisions(document):
            raise DocumentConflict("页面已变化，请刷新后重新选择")

    def _blank_undo_record(self, document):
        record = document.get("last_blank_cleanup")
        if not record:
            return None
        # Renaming is independent of page edits. Image replacements and offline
        # corrections change image revisions even when the order stays the same.
        if (record["order_revision"] != document["order_revision"]
                or record["after_order"] != document["page_order"]
                or record["page_revisions"] != self._page_revisions(document)):
            return None
        for number in record["removed"]:
            page = document["page_details"].get(str(number), {})
            if (not page.get("ready") or not page.get("deleted")
                    or page.get("revision") != record["removed_revisions"][str(number)]):
                return None
        return record

    def analyze_blank_pages(self, document_id):
        """Read immutable images outside the lock; stale decisions never mark pages."""
        with self.lock:
            document = self._document(document_id)
            order_revision = document["order_revision"]
            revisions = self._page_revisions(document)
            self._check_page_snapshot(document, order_revision, revisions)
            pages = self._visible_pages(document)
            directory = self.directory(document_id)
            paths = [(number, directory / document["page_details"][str(number)].get("file", f"p{number}.jpg"))
                     for number in pages]
        candidates, errors = [], []
        for position, (number, path) in enumerate(paths, 1):
            try:
                decision = self.blank_analyzer(path.read_bytes())
                if not isinstance(decision, dict) or type(decision.get("is_blank")) is not bool:
                    raise ValueError("分析未返回明确结果")
                if decision["is_blank"]:
                    candidates.append({"page": number, "position": position, "revision": revisions[str(number)]})
            except Exception as exc:
                # A failed read or uncertain analysis retains the page. The
                # caller may still explicitly select it after looking at it.
                errors.append({"page": number, "reason": "页面无法完成分析，已保留：" + type(exc).__name__})
        with self.lock:
            document = self._document(document_id)
            self._check_page_snapshot(document, order_revision, revisions)
            if pages != self._visible_pages(document):
                raise DocumentConflict("页面已变化，请刷新后重新标记")
        return {"document_id": document_id, "order_revision": order_revision,
                "page_revisions": revisions, "candidates": candidates, "examined": len(paths), "errors": errors}

    def delete_selected(self, document_id, pages, *, order_revision, page_revisions):
        """Remove an explicitly selected subset with one durable metadata commit."""
        with self.lock:
            document = self._document(document_id)
            self._check_page_snapshot(document, order_revision, page_revisions)
            current = self._visible_pages(document)
            if (not isinstance(pages, list) or not pages or any(type(n) is not int or n < 1 for n in pages)
                    or len(set(pages)) != len(pages) or not set(pages).issubset(current)):
                raise ValueError("请选择当前页面，页码必须为不重复的正整数")
            selected = set(pages)
            removed = [n for n in current if n in selected]
            candidate = copy.deepcopy(document)
            for number in removed:
                candidate["page_details"][str(number)]["deleted"] = True
            candidate["page_order"] = [n for n in candidate["page_order"] if n not in selected]
            candidate["order_revision"] += 1
            if 1 in selected and candidate["ocr_state"] in ("pending", "running"):
                candidate["ocr_state"] = "cancelled"
            cleanup_id = uuid.uuid4().hex
            candidate["last_blank_cleanup"] = {
                "cleanup_id": cleanup_id, "removed": removed, "prior_order": list(document["page_order"]),
                "after_order": list(candidate["page_order"]), "order_revision": candidate["order_revision"],
                "page_revisions": self._page_revisions(candidate),
                "removed_revisions": {str(n): page_revisions[str(n)] for n in removed},
            }
            self._save(candidate)
            self._documents[document_id] = candidate
            return {"document": self._public(candidate), "removed": removed, "cleanup_id": cleanup_id}

    def undo_blank_cleanup(self, document_id, cleanup_id):
        with self.lock:
            document = self._document(document_id)
            if not isinstance(cleanup_id, str) or not re.fullmatch(r"[a-f0-9]{32}", cleanup_id):
                raise ValueError("缺少有效的删除记录")
            if self.pending_batches():
                raise DocumentConflict("扫描期间不能恢复页面，请等待当前批次结束")
            record = self._blank_undo_record(document)
            if not record or record["cleanup_id"] != cleanup_id:
                raise DocumentConflict("页面已变化或删除记录已撤销，无法恢复此次删除")
            candidate = copy.deepcopy(document)
            for number in record["removed"]:
                candidate["page_details"][str(number)]["deleted"] = False
            candidate["page_order"] = list(record["prior_order"])
            candidate["order_revision"] += 1
            candidate.pop("last_blank_cleanup")
            self._save(candidate)
            self._documents[document_id] = candidate
            return {"document": self._public(candidate), "restored": list(record["removed"])}

    def _upgrade_order(self, document):
        changed = "page_order" not in document or "order_revision" not in document
        document.setdefault("page_order", sorted(int(n) for n, page in document["page_details"].items()
                                                 if not page.get("deleted")))
        document.setdefault("order_revision", 0)
        for page in document["page_details"].values():
            if "revision" not in page:
                page["revision"] = 1
                changed = True
        return changed

    def _upgrade_naming(self, document, *, recover=True):
        changed = any(key not in document for key in ("name_source", "auto_name", "ocr_state"))
        # Existing names are user-owned. An old document must never be renamed
        # merely because the service gained OCR support.
        document.setdefault("name_source", "manual")
        document.setdefault("auto_name", document["name_source"] != "manual")
        document.setdefault("ocr_state", "pending" if document["name_source"] == "default" else "skipped")
        if recover and document["name_source"] == "default" and document["ocr_state"] == "running":
            document["ocr_state"] = "pending"
            changed = True
        return changed

    def _unique_name(self, name, *, exclude=None):
        def key(value):
            return unicodedata.normalize("NFC", value).casefold()
        used = {key(document["name"]) for sid, document in self._documents.items()
                if sid != exclude and not document.get("deleted_at")}
        if key(name) not in used:
            return name
        number = 1
        while True:
            suffix = f"-{number:03d}"
            candidate = name[:120 - len(suffix)] + suffix
            if key(candidate) not in used:
                return candidate
            number += 1

    def create(self, name=None, *, dpi=150, duplex=False, document_id=None, auto_name=None,
               render_mode="original"):
        options = scan_options(dpi, duplex)
        render_mode = validate_render_mode(render_mode)
        if auto_name is not None and type(auto_name) is not bool:
            raise ValueError("auto_name 必须是布尔值")
        auto_name = name is None if auto_name is None else auto_name
        if auto_name or name is None:
            name = time.strftime("扫描文件 %Y-%m-%d %H.%M.%S")
        else:
            name = document_name(name)
        with self.lock:
            document_id = document_id or new_id()
            directory = self.directory(document_id)
            if document_id in self._documents or directory.exists():
                raise ValueError("文件标识已存在")
            document = {"version": 1, "id": document_id, "name": self._unique_name(name),
                        "state": "done", "msg": "尚无扫描页面", "created_at": timestamp(),
                        "updated_at": timestamp(), **options, "next_page": 1,
                        "name_source": "default" if auto_name else "manual", "auto_name": auto_name,
                        "render_mode": render_mode,
                        "ocr_state": "pending" if auto_name else "skipped",
                        "page_details": {}, "batches": [], "page_order": [], "order_revision": 0}
            durable_mkdir(directory)
            self._save(document)
            self._documents[document_id] = document
            return self._public(document)

    def get(self, document_id):
        with self.lock:
            return self._public(self._document(document_id))

    def list(self):
        with self.lock:
            return [self._public(d) for d in sorted(
                (d for d in self._documents.values() if not d.get("deleted_at")),
                key=lambda d: (datetime.fromisoformat(d["updated_at"].replace("Z", "+00:00")), d["id"]),
                reverse=True)]

    def delete_document(self, document_id, *, updated_at):
        """Hide a reviewed idle document with one durable tombstone commit."""
        if not isinstance(updated_at, str) or not updated_at.strip():
            raise ValueError("请提供当前文件的更新时间")
        with self.lock:
            document = self._document(document_id)
            if document["state"] == "scanning" or any(b["pending"] for b in document["batches"]):
                raise DocumentConflict("该文件正在扫描或等待扫描结束，暂不能删除")
            if updated_at != document["updated_at"]:
                raise DocumentConflict("文件已变化，请刷新后重新确认删除")
            candidate = copy.deepcopy(document)
            candidate["deleted_at"] = timestamp()
            if candidate["ocr_state"] in ("pending", "running"):
                candidate["ocr_state"] = "cancelled"
            self._save(candidate)
            self._documents[document_id] = candidate
            return {"ok": True, "id": document_id}

    def set_render_mode(self, document_id, mode, *, updated_at):
        mode = validate_render_mode(mode)
        if not isinstance(updated_at, str) or not updated_at.strip():
            raise ValueError("请提供当前文件的更新时间")
        with self.lock:
            document = self._document(document_id)
            if updated_at != document["updated_at"]:
                raise DocumentConflict("文件已变化，请刷新后重新选择图片模式")
            if mode == document.get("render_mode", "original"):
                return self._public(document)
            candidate = copy.deepcopy(document)
            candidate["render_mode"] = mode
            self._save(candidate)
            self._documents[document_id] = candidate
            return self._public(candidate)

    def rename(self, document_id, name):
        name = document_name(name)
        with self.lock:
            document = self._document(document_id)
            document["name"] = self._unique_name(name, exclude=document_id)
            self._lock_name(document)
            self._save(document)
            return self._public(document)

    def _lock_name(self, document):
        document.update(name_source="manual", auto_name=False)
        if document["ocr_state"] in ("pending", "running"):
            document["ocr_state"] = "cancelled"

    def lock_name(self, document_id):
        with self.lock:
            document = self._document(document_id)
            self._lock_name(document)
            self._save(document)
            return self._public(document)

    def ocr_eligible(self, document_id):
        with self.lock:
            self.directory(document_id)
            document = self._documents.get(document_id)
            if document is None or document.get("deleted_at"):
                return False
            first_page = document["page_details"].get("1", {})
            return (document["name_source"] == "default" and document["auto_name"]
                    and document["ocr_state"] == "pending"
                    and first_page.get("ready") and not first_page.get("deleted"))

    def pending_ocr(self):
        with self.lock:
            return [sid for sid in self._documents if self.ocr_eligible(sid)]

    def begin_ocr(self, document_id):
        with self.lock:
            if not self.ocr_eligible(document_id):
                return None
            document = self._document(document_id)
            document["ocr_state"] = "running"
            self._save(document)
            return self.directory(document_id) / document["page_details"]["1"].get("file", "p1.jpg")

    def finish_ocr(self, document_id, text, *, error=""):
        with self.lock:
            self.directory(document_id)
            document = self._documents.get(document_id)
            if document is None or document.get("deleted_at"):
                return None
            if document["name_source"] != "default" or not document["auto_name"] or document["ocr_state"] != "running":
                return self._public(document)
            name = clean_filename(text)
            if name and not document["page_details"].get("1", {}).get("deleted"):
                document["name"] = self._unique_name(name, exclude=document_id)
                document.update(name_source="ocr", ocr_state="done")
            else:
                document["ocr_state"] = "failed"
                document["ocr_error"] = error or "未识别到首行文字"
            self._save(document)
            return self._public(document)

    def pending_batches(self):
        with self.lock:
            return [(d["id"], copy.deepcopy(b)) for d in self._documents.values()
                    if not d.get("deleted_at") for b in d["batches"] if b["pending"]]

    def reorder(self, document_id, pages, order_revision):
        with self.lock:
            document = self._document(document_id)
            if self.pending_batches():
                raise DocumentConflict("扫描期间不能调整页序，请等待当前批次结束")
            if type(order_revision) is not int:
                raise ValueError("缺少有效的页序版本")
            if order_revision != document["order_revision"]:
                raise DocumentConflict("页面顺序已更新，请刷新后重试")
            current = self._public(document)["pages"]
            if (not isinstance(pages, list) or any(type(n) is not int for n in pages)
                    or len(pages) != len(current) or len(set(pages)) != len(pages)
                    or set(pages) != set(current)):
                raise ValueError("页序必须包含当前全部页面且不能重复")
            if pages != current:
                document = copy.deepcopy(document)
                unpublished = [n for n in document["page_order"] if n not in current]
                document["page_order"] = list(pages) + unpublished
                document["order_revision"] += 1
                self._save(document)
                self._documents[document_id] = document
            return self._public(document)

    def start_batch(self, document_id, batch_id=None, *, dpi=150, duplex=False, target_page=None):
        options = scan_options(dpi, duplex)
        with self.lock:
            document = self._document(document_id)
            if self.pending_batches():
                raise ValueError("已有扫描任务尚未结束")
            batch_id = batch_id or new_id()
            if not ID_RE.fullmatch(batch_id):
                raise ValueError("无效批次标识")
            if target_page is not None:
                page = document["page_details"].get(str(target_page))
                if type(target_page) is not int or not page or not page.get("ready") or page.get("deleted"):
                    raise KeyError(target_page)
                options = scan_options(page["dpi"], False)
            batch = {"id": batch_id, **options, "base_page": document["next_page"],
                     "source_pages": {}, "pending": True, "dispatch": "prepared",
                     "started_at": timestamp(), "state": "scanning", "terminal_status": None}
            if target_page is not None:
                batch.update(target_page=target_page, target_revision=page["revision"],
                             max_pages=1, replacement=None)
                if document["ocr_state"] in ("pending", "running"):
                    document["ocr_state"] = "cancelled"
            document["batches"].append(batch)
            if target_page is None:
                document.update(**options, state="scanning", msg="正在等待扫描页面…")
            else:
                document.update(state="scanning", msg="正在重扫本页，完成前保留原页…")
            self._save(document)
            return copy.deepcopy(batch)

    def _batch(self, document, batch_id):
        return next(b for b in document["batches"] if b["id"] == batch_id)

    def batch(self, document_id, batch_id):
        with self.lock:
            return copy.deepcopy(self._batch(self._document(document_id), batch_id))

    def update_batch(self, document_id, batch_id, **changes):
        with self.lock:
            document = self._document(document_id)
            self._batch(document, batch_id).update(changes)
            self._save(document)

    def waiting(self, document_id, batch_id, message):
        """Keep the physical device reserved until its terminal status arrives."""
        with self.lock:
            document = self._document(document_id)
            batch = self._batch(document, batch_id)
            if batch["pending"] and document.get("msg") != message:
                batch["state"] = "waiting"
                document.update(state="error", msg=message)
                self._save(document)

    def abandon_batch(self, document_id, batch_id, message, *, withdrawn):
        """Release a reserved device when an operator forces a batch to end.

        The batch stops being pending so the station accepts new work again.
        No terminal status is invented: the document records that a person
        ended the batch, and pages already published stay untouched.
        ``withdrawn`` records whether the physical request was confirmed gone
        from the scanner host, so an unreachable host leaves retryable work
        behind instead of a request that scans by surprise later.
        """
        with self.lock:
            document = self._document(document_id)
            batch = self._batch(document, batch_id)
            if not batch["pending"]:
                return self._public(document)
            batch.update(pending=False, state="cancelled", cancelled_at=timestamp(),
                         request_withdrawn=bool(withdrawn))
            document.update(state="cancelled", msg=message)
            self._save(document)
            return self._public(document)

    def unwithdrawn_batches(self):
        """Cancelled batches whose physical request may still be queued."""
        with self.lock:
            return [(d["id"], b["id"]) for d in self._documents.values()
                    if not d.get("deleted_at")
                    for b in d["batches"]
                    if b.get("cancelled_at") and not b.get("request_withdrawn")]

    def mark_request_withdrawn(self, document_id, batch_id):
        with self.lock:
            document = self._document(document_id)
            batch = self._batch(document, batch_id)
            if batch.get("request_withdrawn"):
                return False
            batch["request_withdrawn"] = True
            self._save(document)
            return True

    def received(self, document_id, batch_id, source_page):
        with self.lock:
            document = self._document(document_id)
            batch = self._batch(document, batch_id)
            if batch.get("target_page") is not None:
                replacement = batch.get("replacement")
                return source_page == 1 and bool(replacement and replacement.get("ready"))
            page_number = batch["source_pages"].get(str(source_page))
            if page_number is None:
                return False
            page = document["page_details"][str(page_number)]
            return bool(page.get("ready") or page.get("deleted"))

    def add_page(self, document_id, batch_id, source_page, data):
        if type(source_page) is not int or source_page < 1 or source_page > 100000:
            raise ValueError("无效页码")
        complete_jpeg(data)
        with self.lock:
            document = self._document(document_id)
            batch = self._batch(document, batch_id)
            if batch.get("target_page") is not None:
                return self._stage_replacement(document, batch, source_page, data)
            source_key = str(source_page)
            number = batch["source_pages"].get(source_key)
            if number is not None:
                page = document["page_details"][str(number)]
                if page.get("ready") or page.get("deleted"):
                    return number
            else:
                # Source numbers define ordering even if SMB lists p2 before p1.
                number = batch["base_page"] + source_page - 1
                batch["source_pages"][source_key] = number
                document["next_page"] = max(document["next_page"], number + 1)
                page = {"batch_id": batch_id, "source_page": source_page,
                        "dpi": batch["dpi"], "duplex": batch["duplex"],
                        "ready": False, "deleted": False, "revision": 1}
                document["page_details"][str(number)] = page
                batch_numbers = set(batch["source_pages"].values())
                document["page_order"] = [n for n in document["page_order"] if n not in batch_numbers]
                document["page_order"].extend(sorted(n for n in batch_numbers
                                                     if not document["page_details"][str(n)].get("deleted")))
                self._save(document)
            directory = self.directory(document_id)
        # Decoding, paper-edge detection and both JPEG writes are the expensive
        # part of receiving a page. They run outside the store lock so a page in
        # progress cannot stall unrelated API reads (document lists, page
        # status) for the duration of the whole page.
        atomic_write(directory / "raw" / f"p{number}.jpg", data)
        processed, crop = self.processor(data)
        width, height = complete_jpeg(processed)
        atomic_write(directory / f"p{number}.jpg", processed)
        with self.lock:
            document = self._document(document_id)
            page = document["page_details"][str(number)]
            if page.get("ready") or page.get("deleted"):
                return number
            page.update(ready=True, crop=crop, width=width, height=height)
            document["order_revision"] += 1
            document.update(state="scanning", msg="正在扫描，已收到的页面已保存")
            self._save(document)
        # The image and metadata are already durable and visible. This callback
        # only enqueues OCR; CPU work runs outside the store/collection locks.
        if self.on_page_published is not None:
            self.on_page_published(document_id, number)
        return number

    def _stage_replacement(self, document, batch, source_page, data):
        if source_page != 1:
            raise ValueError("重扫本页只允许接收一页")
        if batch.get("replacement", {}) and batch["replacement"].get("ready"):
            return batch["target_page"]
        number = batch["target_page"]
        revision = batch["target_revision"] + 1
        relative = Path("revisions") / f"p{number}" / f"r{revision}-{batch['id']}"
        directory = self.directory(document["id"])
        atomic_write(directory / relative / "raw.jpg", data)
        processed, crop = self.processor(data)
        width, height = complete_jpeg(processed)
        atomic_write(directory / relative / "page.jpg", processed)
        batch["source_pages"]["1"] = number
        batch["replacement"] = {
            "file": str(relative / "page.jpg"), "raw_file": str(relative / "raw.jpg"),
            "ready": True, "revision": revision, "dpi": batch["dpi"], "duplex": False,
            "width": width, "height": height, "crop": crop,
            "batch_id": batch["id"], "source_page": 1}
        document.update(state="scanning", msg="重扫页面已接收，正在确认完成；原页仍保留")
        self._save(document)
        return number

    def _commit_replacement(self, document, batch):
        replacement = batch.get("replacement")
        if not replacement or not replacement.get("ready"):
            return "重扫未收到完整页面，原页已保留"
        number = batch["target_page"]
        page = document["page_details"].get(str(number))
        if not page or page.get("deleted") or page["revision"] != batch["target_revision"]:
            return "原页面已发生变化，重扫结果未替换"
        directory = self.directory(document["id"])
        complete_jpeg((directory / replacement["file"]).read_bytes())
        complete_jpeg((directory / replacement["raw_file"]).read_bytes())
        self._keep_revision(page, number)
        page.update(replacement)
        batch["replacement_committed"] = True
        return ""

    @staticmethod
    def _keep_revision(page, number):
        previous = {key: copy.deepcopy(value) for key, value in page.items() if key != "revisions"}
        previous.setdefault("file", f"p{number}.jpg")
        previous.setdefault("raw_file", f"raw/p{number}.jpg")
        page.setdefault("revisions", []).append(previous)

    def reprocess_pages(self, document_id=None, *, dry_run=False, processing_version=2):
        """Offline correction upgrade; preserve document ordering/name/OCR state."""
        with self.lock:
            if self.pending_batches():
                raise DocumentConflict("仍有未结束扫描，不能重处理历史页面")
            selected = ([document_id] if document_id is not None else
                        [sid for sid, document in self._documents.items() if not document.get("deleted_at")])
            report = []
            for sid in selected:
                document = self._document(sid)
                for number in self._public(document)["pages"]:
                    document = self._document(sid)
                    page = document["page_details"][str(number)]
                    crop = page.get("crop") or {}
                    version = crop.get("processing_version", 0)
                    item = {"id": sid, "page": number, "revision": page["revision"]}
                    if isinstance(version, int) and version >= processing_version:
                        report.append(dict(item, action="skip", reason="已使用当前处理版本"))
                        continue
                    if dry_run:
                        report.append(dict(item, action="would_reprocess", next_revision=page["revision"] + 1))
                        continue
                    try:
                        raw_file = page.get("raw_file", f"raw/p{number}.jpg")
                        raw = (self.directory(sid) / raw_file).read_bytes()
                        complete_jpeg(raw)
                        processed, crop = self.processor(raw)
                        width, height = complete_jpeg(processed)
                        relative = Path("revisions") / f"p{number}" / (
                            f"r{page['revision'] + 1}-reprocess-{uuid.uuid4().hex[:12]}") / "page.jpg"
                        atomic_write(self.directory(sid) / relative, processed)
                        candidate = copy.deepcopy(document)
                        replacement = candidate["page_details"][str(number)]
                        self._keep_revision(replacement, number)
                        replacement.update(file=str(relative), raw_file=raw_file, revision=page["revision"] + 1,
                                           width=width, height=height, crop=crop, reprocessed_at=timestamp())
                        self._save(candidate, touch=False)
                        self._documents[sid] = candidate
                    except Exception as exc:
                        report.append(dict(item, action="failed", error=f"{type(exc).__name__}: {exc}"))
                    else:
                        report.append(dict(item, action="reprocessed", next_revision=replacement["revision"]))
            return report

    def finish_batch(self, document_id, batch_id=None, error=""):
        with self.lock:
            document = self._document(document_id)
            if batch_id is None:
                batch_id = next(b["id"] for b in document["batches"] if b["pending"])
            batch = self._batch(document, batch_id)
            if not batch["pending"]:
                return self._public(document)
            is_rescan = batch.get("target_page") is not None
            if is_rescan:
                # Immutable image versions are already on disk. One atomic JSON
                # pointer switch publishes both raw and corrected images together;
                # a failed metadata write leaves the old in-memory page visible.
                document = copy.deepcopy(document)
                batch = self._batch(document, batch_id)
                if not error:
                    error = self._commit_replacement(document, batch)
                if error:
                    error += "；原页已保留" if "原页" not in error else ""
            batch.update(pending=False, state="error" if error else "done", finished_at=timestamp())
            document.update(state="error" if error else "done",
                            msg=error or ("本页已替换，原版本已保留" if is_rescan else "扫描完成，页面已保存"))
            self._save(document)
            if is_rescan:
                self._documents[document_id] = document
            return self._public(document)

    def delete_page(self, document_id, number):
        with self.lock:
            document = self._document(document_id)
            page = document["page_details"].get(str(number))
            if not page or not page.get("ready") or page.get("deleted"):
                raise KeyError(number)
            if any(b["pending"] and b.get("target_page") is not None for b in document["batches"]):
                raise DocumentConflict("重扫本页期间不能删除页面，请等待重扫结束")
            # Keep the mapping/tombstone and original: retries cannot resurrect it.
            page["deleted"] = True
            document["page_order"] = [n for n in document["page_order"] if n != number]
            document["order_revision"] += 1
            if number == 1 and document["ocr_state"] in ("pending", "running"):
                document["ocr_state"] = "cancelled"
            self._save(document)
            # Version files are retained for recovery; metadata hides the page.
            return self._public(document)

    def page_bytes(self, document_id, number, *, original=False):
        """Read the corrected base or raw original, independently of rendering."""
        with self.lock:
            document = self._document(document_id)
            page = document["page_details"].get(str(number))
            if not page or not page.get("ready") or page.get("deleted"):
                raise KeyError(number)
            directory = self.directory(document_id)
            relative = page.get("raw_file" if original else "file",
                                f"raw/p{number}.jpg" if original else f"p{number}.jpg")
            return (directory / relative).read_bytes()

    def _render_snapshot(self, document, number):
        """Capture an immutable source pointer while holding the metadata lock."""
        page = document["page_details"].get(str(number))
        if not page or not page.get("ready") or page.get("deleted"):
            raise KeyError(number)
        return {"id": document["id"], "number": number, "revision": page["revision"],
                "file": page.get("file", f"p{number}.jpg"), "dpi": page["dpi"],
                "size": (page.get("width"), page.get("height")), "render_version": ENHANCEMENT_VERSION}

    def _check_render_snapshot(self, snapshot):
        with self.lock:
            current = self._render_snapshot(self._document(snapshot["id"]), snapshot["number"])
            if (current["revision"], current["file"]) != (snapshot["revision"], snapshot["file"]):
                raise DocumentConflict("页面已更新，请刷新后重试")

    def _render_snapshot_bytes(self, snapshot, mode):
        directory = self.directory(snapshot["id"])
        source_path = directory / snapshot["file"]
        if mode == "original":
            self._check_render_snapshot(snapshot)
            return source_path.read_bytes()
        source_key = hashlib.sha256(snapshot["file"].encode("utf-8")).hexdigest()[:16]
        cache = (directory / "render-cache" / f"v{snapshot['render_version']}" /
                 f"p{snapshot['number']}" / f"r{snapshot['revision']}-{source_key}-{mode}.jpg")
        with self.render_lock:
            # Queued requests for deleted or replaced pages do no image work.
            self._check_render_snapshot(snapshot)
            expected_size = snapshot["size"]
            source = None
            if any(type(value) is not int or value < 1 for value in expected_size):
                source = source_path.read_bytes()
                expected_size = complete_jpeg(source)
            try:
                cached = cache.read_bytes()
                if complete_jpeg(cached) == expected_size:
                    return cached
            except (OSError, ValueError):
                pass  # Derived caches can always be rebuilt from the base.
            source = source if source is not None else source_path.read_bytes()
            expected_size = complete_jpeg(source)
            rendered = self.enhancer(source, mode)
            if complete_jpeg(rendered) != expected_size:
                raise ValueError("图片增强改变了页面尺寸，已保留原图")
            self._check_render_snapshot(snapshot)
            try:
                atomic_write(cache, rendered)
            except OSError:
                # The rendering is still valid when optional caching fails.
                # No metadata or source image depends on this cache write.
                pass
            return rendered

    def rendered_page_bytes(self, document_id, number, *, mode=None, revision=None, render_version=None):
        for value in (revision, render_version):
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError("无效图片版本")
        with self.lock:
            document = self._document(document_id)
            mode = validate_render_mode(document.get("render_mode", "original") if mode is None else mode)
            snapshot = self._render_snapshot(document, number)
            if revision is not None and revision != snapshot["revision"]:
                raise DocumentConflict("页面已更新，请刷新后重试")
            if render_version is not None and render_version != snapshot["render_version"]:
                raise DocumentConflict("图片增强已更新，请刷新后重试")
        data = self._render_snapshot_bytes(snapshot, mode)
        self._check_render_snapshot(snapshot)
        return data

    def _recover_pages(self, document):
        if document.get("deleted_at"):
            return
        directory = self.directory(document["id"])
        changed = False
        for number, page in document["page_details"].items():
            if page.get("deleted"):
                continue
            processed_path = directory / page.get("file", f"p{number}.jpg")
            raw_path = directory / page.get("raw_file", f"raw/p{number}.jpg")
            if page.get("ready") and self._valid_revision(directory, page, number):
                continue
            previous = next((record for record in reversed(page.get("revisions", []))
                             if self._valid_revision(directory, record, number)), None)
            if previous is not None:
                old_revision = page.get("revision", 1)
                history = copy.deepcopy(page.get("revisions", []))
                failed = {key: copy.deepcopy(value) for key, value in page.items() if key != "revisions"}
                failed.update(ready=False, recovery_error="文件缺失或 JPEG 损坏")
                history.append(failed)
                restored = copy.deepcopy(previous)
                restored.update(ready=True, revision=old_revision + 1, revisions=history,
                                recovered_from_revision=previous.get("revision", 1),
                                recovery_error="当前版本损坏，已恢复上一完整版本")
                document["page_details"][number] = restored
                document["msg"] = "检测到损坏的页面版本，已恢复上一完整版本"
                changed = True
                continue
            try:
                data = raw_path.read_bytes()
                complete_jpeg(data)
                processed, crop = self.processor(data)
                width, height = complete_jpeg(processed)
                if page.get("ready"):
                    # A valid raw can repair a page with no usable prior pair.
                    # Keep the damaged file for inspection and publish a new URL.
                    self._keep_revision(page, int(number))
                    relative = Path("revisions") / f"p{number}" / (
                        f"r{page['revision'] + 1}-recovery-{uuid.uuid4().hex[:12]}") / "page.jpg"
                    atomic_write(directory / relative, processed)
                    page.update(file=str(relative), raw_file=str(raw_path.relative_to(directory)),
                                revision=page["revision"] + 1)
                else:
                    atomic_write(processed_path, processed)
            except Exception:
                changed = changed or bool(page.get("ready"))
                page["ready"] = False
                continue
            page.update(ready=True, crop=crop, width=width, height=height)
            if int(number) not in document["page_order"]:
                document["page_order"].append(int(number))
            document["order_revision"] += 1
            changed = True
        if any(b["pending"] for b in document["batches"]):
            document.update(state="scanning", msg="服务已恢复，正在继续收集上次扫描页面…")
            changed = True
        if changed:
            self._save(document)

    @staticmethod
    def _valid_revision(directory, page, number):
        try:
            complete_jpeg((directory / page.get("file", f"p{number}.jpg")).read_bytes())
            complete_jpeg((directory / page.get("raw_file", f"raw/p{number}.jpg")).read_bytes())
        except (OSError, ValueError):
            return False
        return True

    def migrate_legacy(self, legacy_root):
        """Import old sessions without altering any source files, safe to rerun."""
        source_root = Path(legacy_root)
        if not source_root.is_dir() or source_root.resolve() == self.root.resolve():
            return
        with self.lock:
            for source in sorted(source_root.iterdir()):
                if not source.is_dir() or not ID_RE.fullmatch(source.name):
                    continue
                paths = sorted((p for p in source.iterdir() if re.fullmatch(r"p[1-9][0-9]*\.jpg", p.name)),
                               key=lambda p: int(p.stem[1:]))
                if not paths:
                    continue
                existing = self._documents.get(source.name)
                if existing and (existing.get("deleted_at")
                                 or existing.get("migrated_from") != str(source.resolve())):
                    continue
                if not existing:
                    self.create("历史扫描 " + source.name, document_id=source.name)
                    document = self._documents[source.name]
                    document["migrated_from"] = str(source.resolve())
                    document["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(source.stat().st_mtime))
                    self._save(document)
                document = self._documents[source.name]
                imported = False
                for path in paths:
                    number = int(path.stem[1:])
                    existing_page = document["page_details"].get(str(number))
                    if existing_page and (existing_page.get("deleted") or not existing_page.get("legacy")
                                          or "crop" in existing_page):
                        continue
                    data = path.read_bytes()
                    destination = self.directory(source.name)
                    # Preserve even incomplete legacy bytes in raw for manual recovery.
                    atomic_write(destination / "raw" / path.name, data)
                    try:
                        width, height = complete_jpeg(data)
                    except (OSError, ValueError):
                        ready = False
                        width, height = 0, 0
                        crop = {"method": "original", "processing_error": "历史 JPEG 未完整写入"}
                    else:
                        try:
                            processed, crop = self.processor(data)
                            width, height = complete_jpeg(processed)
                        except Exception as exc:
                            # Import remains useful if one legacy page cannot be
                            # corrected; its exact original is always preserved.
                            processed = data
                            width, height = complete_jpeg(data)
                            crop = {"method": "original", "processing_error": type(exc).__name__}
                        atomic_write(destination / path.name, processed)
                        ready = True
                    document["page_details"][str(number)] = {
                        "dpi": 150, "duplex": False, "ready": ready, "deleted": False,
                        "width": width, "height": height, "legacy": True, "crop": crop, "revision": 1}
                    if number not in document["page_order"]:
                        document["page_order"].append(number)
                        document["order_revision"] += 1
                    document["next_page"] = max(document["next_page"], number + 1)
                    self._save(document)
                    imported = True
                if imported:
                    document["msg"] = "历史扫描已保存"
                    self._save(document)

    def pdf(self, document_id):
        """Render a fixed mode/order/name snapshot without blocking collection."""
        with self.lock:
            document = self._document(document_id)
            numbers = self._visible_pages(document)
            if not numbers:
                raise ValueError("没有页面可导出")
            snapshots = [self._render_snapshot(document, number) for number in numbers]
            mode = validate_render_mode(document.get("render_mode", "original"))
            name = document["name"]
        pages = [(self._render_snapshot_bytes(snapshot, mode), snapshot["dpi"]) for snapshot in snapshots]
        data = make_pdf(pages)
        with self.lock:
            for snapshot in snapshots:
                self._check_render_snapshot(snapshot)
        return data, name


def make_pdf(pages):
    """A PDF 1.4 catalog/pages tree and one JPEG XObject per page, no extra dependency."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    kids = []
    for jpeg, dpi in pages:
        with Image.open(io.BytesIO(jpeg)) as im:
            im.load()
            width, height = im.size
            color_space = "DeviceGray" if im.mode == "L" else "DeviceRGB"
            if im.mode not in ("RGB", "L"):
                output = io.BytesIO()
                im.convert("RGB").save(output, "JPEG", quality=95)
                jpeg = output.getvalue()
        page_id = len(objects) + 1
        image_id, content_id = page_id + 1, page_id + 2
        kids.append(f"{page_id} 0 R")
        w_pt, h_pt = width * 72 / dpi, height * 72 / dpi
        objects.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w_pt:.5f} {h_pt:.5f}] "
                        f"/Resources << /XObject << /Im0 {image_id} 0 R >> >> "
                        f"/Contents {content_id} 0 R >>").encode("ascii"))
        objects.append((f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
                        f"/ColorSpace /{color_space} /BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpeg)} >>\nstream\n").encode("ascii")
                       + jpeg + b"\nendstream")
        content = f"q {w_pt:.5f} 0 0 {h_pt:.5f} 0 0 cm /Im0 Do Q\n".encode("ascii")
        objects.append(f"<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"endstream")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode("ascii")
    result = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for n, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{n} 0 obj\n".encode("ascii") + obj + b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    result.extend((f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n").encode("ascii"))
    return bytes(result)
