#!/usr/bin/env python3
"""Persistent documents and incremental Windows SMB collection.

Importing performs no network I/O or data-directory creation. Only explicit
POST /scan and /continue actions create a Windows request.
"""
from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from impacket.smbconnection import SMBConnection
from configuration import apply_environment_file, configured_port
from documents import DocumentConflict, DocumentStore, document_name, scan_options, validate_render_mode
from scanner_status import readiness_problem

apply_environment_file()

LISTEN_HOST = os.environ.get("SCAN_STATION_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = 8081
SCANNER_HOST = os.environ.get("SCAN_STATION_SCANNER_HOST", "").strip()
SMB_USER = os.environ.get("SCAN_STATION_SMB_USER", "").strip()
SMB_PASS = os.environ.get("SCAN_STATION_SMB_PASSWORD", "")
REQ_DIR = "Users/Public/scan-agent/req"
OUT_DIR = "Users/Public/scan-agent/out"
SCANNER_STATUS_PATH = "Users/Public/scan-agent/scanner-status.json"
DATA_ROOT = os.environ.get("SCAN_STATION_DATA_DIR") or "/var/lib/scan-station/documents"
LEGACY_ROOT = os.environ.get("SCAN_STATION_LEGACY_DIR") or "/tmp/scan-station"
SCAN_TIMEOUT = 300
HERE = Path(__file__).resolve().parent
LOG = logging.getLogger("scan-station")


def smb():
    missing = [name for name, value in (
        ("SCAN_STATION_SCANNER_HOST", SCANNER_HOST),
        ("SCAN_STATION_SMB_USER", SMB_USER),
        ("SCAN_STATION_SMB_PASSWORD", SMB_PASS)) if not value]
    if missing:
        raise RuntimeError("请在本机环境文件中配置：" + "、".join(missing))
    connection = SMBConnection(SCANNER_HOST, SCANNER_HOST, timeout=15)
    try:
        connection.login(SMB_USER, SMB_PASS)
    except Exception:
        connection.close()
        raise
    return connection


def smb_read(connection, path):
    buffer = io.BytesIO()
    connection.getFile("C$", path, buffer.write)
    return buffer.getvalue()


def smb_write(connection, path, data):
    buffer = io.BytesIO(data)
    connection.putFile("C$", path, buffer.read)


def smb_list(connection, path):
    # A failed listing must not be mistaken for an observed empty directory.
    return [entry.get_longname() for entry in connection.listPath("C$", path + "/*")]


def smb_delete(connection, path):
    connection.deleteFile("C$", path)


def request_names(batch_id):
    """Both filenames a batch can occupy in the request directory.

    ``.upload`` exists only while a request is being published; the agent reads
    ``.json`` alone, so an interrupted publish is invisible to the scanner and
    must be cleaned up by the host that wrote it.
    """
    return (f"{batch_id}.json", f"{batch_id}.upload")


def read_scanner_status():
    connection = smb()
    try:
        return smb_read(connection, SCANNER_STATUS_PATH)
    finally:
        connection.close()


def terminal_result(text):
    text = text.lstrip("\ufeff").strip()
    if not text:
        return None
    if text == "ok":
        return {"error": "", "count": None, "text": text}
    match = re.fullmatch(r"ok\s*(?::|\s)\s*(?:pages\s*[=:]\s*)?(\d+)", text, re.I)
    if match:
        return {"error": "", "count": int(match.group(1)), "text": text}
    counted_error = re.fullmatch(r"error\s+pages=(\d+)\s*:\s*(.*)", text, re.I | re.S)
    if counted_error:
        return {"error": counted_error.group(2) or "扫描失败", "count": int(counted_error.group(1)), "text": text}
    if text.lower().startswith("error"):
        return {"error": re.sub(r"^error\s*:?\s*", "", text, flags=re.I) or "扫描失败",
                "count": None, "text": text}
    return None  # Retry partial status writes such as `o` or `err`.


class ScanBusy(Exception):
    def __init__(self, active_id):
        self.active_id = active_id
        super().__init__("已有扫描任务正在进行，请等待当前批次结束")


class ScanCoordinator:
    def __init__(self, store, *, connection_factory=smb, timeout=SCAN_TIMEOUT,
                 poll_seconds=2, worker_launcher=None, clock=time.monotonic):
        self.store = store
        self.connection_factory = connection_factory
        self.timeout = timeout
        self.poll_seconds = poll_seconds
        self.clock = clock
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.threads = {}
        self.worker_launcher = worker_launcher or self._launch_thread

    @property
    def active_id(self):
        pending = self.store.pending_batches()
        return pending[0][0] if pending else None

    def _launch_thread(self, target, args):
        worker = threading.Thread(target=target, args=args, daemon=True)
        worker.start()
        return worker

    def _launch(self, document_id, batch_id):
        self.threads[batch_id] = self.worker_launcher(self.collect, (document_id, batch_id))

    def recover(self):
        """Resume persisted batches without resubmitting ambiguous requests."""
        with self.lock:
            for document_id, batch in self.store.pending_batches():
                if batch["id"] not in self.threads:
                    self._launch(document_id, batch["id"])

    def _withdraw_request(self, batch_id):
        """Best-effort removal of a queued physical scan request.

        Returns True only when a fresh listing confirms the request files are
        gone. False means the scanner host could not be reached or the delete
        did not stick, so the request may still be scanned when the agent next
        starts; the caller keeps it queued for retry rather than assuming the
        machine is free.
        """
        connection = None
        try:
            connection = self.connection_factory()
            names = request_names(batch_id)
            for name in names:
                if name in smb_list(connection, REQ_DIR):
                    smb_delete(connection, f"{REQ_DIR}/{name}")
            remaining = smb_list(connection, REQ_DIR)
            return not any(name in remaining for name in names)
        except Exception:
            return False
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def abandon(self, document_id):
        """Force-end the batch reserving ``document_id``.

        The physical request is withdrawn first so the scanner agent cannot
        start an abandoned job later, then the reservation is released so the
        station accepts new work. Already-received pages are kept.
        """
        with self.lock:
            pending = [(d, b) for d, b in self.store.pending_batches() if d == document_id]
            if not pending:
                raise ValueError("当前文件没有正在进行的扫描任务")
            batch_id = pending[0][1]["id"]
        # Network work happens outside the coordinator lock: a slow or
        # unreachable scanner host must not block unrelated scan requests.
        withdrawn = self._withdraw_request(batch_id)
        with self.lock:
            still_pending = any(b["id"] == batch_id for _, b in self.store.pending_batches())
            if not still_pending:
                # The agent reported a terminal status while we were trying to
                # withdraw; the real result wins over the operator's cancel.
                return {"document": self.store.get(document_id), "batch_id": batch_id,
                        "withdrawn": withdrawn, "already_finished": True}
            message = ("已强制终止本批扫描，扫描电脑上的请求已撤回。已收到的页面仍然保留。"
                       if withdrawn else
                       "已强制终止本批扫描，但暂时无法连接扫描电脑，请求尚未撤回；"
                       "恢复连接后会自动重试撤回。已收到的页面仍然保留。")
            document = self.store.abandon_batch(document_id, batch_id, message, withdrawn=withdrawn)
        return {"document": document, "batch_id": batch_id, "withdrawn": withdrawn,
                "already_finished": False}

    def sweep_withdrawals(self):
        """Retry withdrawing requests a force-end could not confirm.

        Returns the batch ids that are now confirmed withdrawn. Runs on the
        service timer so cancelling while the scanner computer is off still
        ends with the request removed once that computer comes back.
        """
        withdrawn = []
        for document_id, batch_id in self.store.unwithdrawn_batches():
            if self._withdraw_request(batch_id):
                self.store.mark_request_withdrawn(document_id, batch_id)
                withdrawn.append(batch_id)
        return withdrawn

    def start_withdrawal_sweeper(self, interval=30.0):
        def run():
            while not self.stop.is_set():
                self.stop.wait(interval)
                if self.stop.is_set():
                    return
                try:
                    self.sweep_withdrawals()
                except Exception:
                    LOG.exception("withdrawal sweep failed")
        worker = threading.Thread(target=run, name="request-withdrawal", daemon=True)
        worker.start()
        return worker

    def start(self, *, document_id=None, name=None, dpi=150, duplex=False, auto_name=None,
              render_mode="original"):
        options = scan_options(dpi, duplex)
        render_mode = validate_render_mode(render_mode)
        if auto_name is not None and type(auto_name) is not bool:
            raise ValueError("auto_name 必须是布尔值")
        if name is not None and auto_name is not True:
            name = document_name(name)
        with self.lock:
            if self.active_id:
                raise ScanBusy(self.active_id)
            if document_id is None:
                document_id = self.store.create(name, auto_name=auto_name, render_mode=render_mode, **options)["id"]
            else:
                self.store.get(document_id)
                if name is not None:
                    self.store.rename(document_id, name)
            batch = self.store.start_batch(document_id, **options)
            initial_document = self.store.get(document_id)
            self._launch(document_id, batch["id"])
            # The immediate response always carries the newly assigned default
            # name, even if a fast test/device and OCR finish before HTTP sends.
            return initial_document

    def rescan(self, document_id, number):
        with self.lock:
            if self.active_id:
                raise ScanBusy(self.active_id)
            batch = self.store.start_batch(document_id, target_page=number)
            initial_document = self.store.get(document_id)
            self._launch(document_id, batch["id"])
            return initial_document

    def delete_document(self, document_id, *, updated_at):
        with self.lock:
            if self.active_id == document_id:
                raise ScanBusy(document_id)
            return self.store.delete_document(document_id, updated_at=updated_at)

    def collect(self, document_id, batch_id):
        """Pull complete JPEGs before terminal status; retain incomplete/late work."""
        connection = None
        batch = self.store.batch(document_id, batch_id)
        last_activity = self.clock()
        observed_sizes = {}
        terminal = terminal_result(batch["terminal_status"]) if batch.get("terminal_status") else None
        stable_terminal_polls = 0
        previous_names = None
        last_error = ""
        page_pattern = re.compile(re.escape(batch_id) + r"-p([1-9][0-9]*)\.jpg\Z", re.I)
        try:
            while not self.stop.is_set():
                try:
                    # A forced end releases the reservation outside this worker.
                    # Stop collecting as soon as that happens so an abandoned
                    # batch cannot keep polling the scanner host forever.
                    if not self.store.batch(document_id, batch_id)["pending"]:
                        return
                    if connection is None:
                        connection = self.connection_factory()
                    batch = self.store.batch(document_id, batch_id)
                    if batch["dispatch"] == "prepared":
                        request_options = {"src": "scan-station", "dpi": batch["dpi"], "duplex": batch["duplex"]}
                        if batch.get("target_page") is not None:
                            request_options["max_pages"] = 1
                        request = json.dumps(request_options).encode("utf-8")
                        temporary = f"{REQ_DIR}/{batch_id}.upload"
                        smb_write(connection, temporary, request)
                        # The agent sees only *.json. Persist BEFORE atomically
                        # publishing: an ambiguous rename must never be retried
                        # as a second physical request after a crash.
                        self.store.update_batch(document_id, batch_id, dispatch="dispatching")
                        connection.rename("C$", temporary, f"{REQ_DIR}/{batch_id}.json")
                        self.store.update_batch(document_id, batch_id, dispatch="submitted")
                        last_activity = self.clock()
                    names = smb_list(connection, OUT_DIR)
                    page_files = sorted((int(match.group(1)), name) for name in names
                                        if (match := page_pattern.fullmatch(name)))
                    all_received = True
                    for number, name in page_files:
                        if self.store.received(document_id, batch_id, number):
                            continue
                        try:
                            data = smb_read(connection, f"{OUT_DIR}/{name}")
                            if len(data) > observed_sizes.get(name, 0):
                                observed_sizes[name] = len(data)
                                last_activity = self.clock()
                            self.store.add_page(document_id, batch_id, number, data)
                            last_activity = self.clock()
                        except Exception as exc:
                            all_received = False
                            last_error = f"第 {number} 页尚未读取完成: {type(exc).__name__}"
                    if f"{batch_id}.status" in names and terminal is None:
                        text = smb_read(connection, f"{OUT_DIR}/{batch_id}.status").decode("utf-8-sig", "replace")
                        terminal = terminal_result(text)
                        if terminal is not None:
                            self.store.update_batch(document_id, batch_id, terminal_status=terminal["text"])
                            last_activity = self.clock()
                    if terminal is not None:
                        if batch.get("target_page") is not None and (terminal["error"] or terminal["count"] != 1):
                            self.store.finish_batch(document_id, batch_id,
                                error=terminal["error"] or "重扫未得到单页成功确认")
                            return
                        if terminal["count"] is not None:
                            all_received = all_received and all(self.store.received(document_id, batch_id, n)
                                                               for n in range(1, terminal["count"] + 1))
                        current_names = tuple(name for _, name in page_files)
                        stable_terminal_polls = stable_terminal_polls + 1 if current_names == previous_names else 0
                        previous_names = current_names
                        # Legacy `ok` has no page count. Require two stable
                        # completed directory observations after its appearance.
                        if all_received and stable_terminal_polls >= 1:
                            self.store.finish_batch(document_id, batch_id, error=terminal["error"])
                            return
                    last_error = "" if all_received else last_error
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    if connection is not None:
                        try:
                            connection.close()
                        except Exception:
                            pass
                        connection = None
                    if self.store.batch(document_id, batch_id)["dispatch"] == "prepared":
                        self.store.finish_batch(document_id, batch_id, error="无法连接扫描电脑: " + last_error)
                        return
                if self.clock() - last_activity >= self.timeout:
                    if terminal is not None:
                        self.store.finish_batch(document_id, batch_id,
                            error=(terminal["error"] + "；" if terminal["error"] else "") +
                            "部分页面未能完整读取，已收到的页面已保存。" + last_error)
                        return
                    self.store.waiting(document_id, batch_id,
                        "暂未收到扫描结束确认，已收到的页面已保存；正在继续等待，为避免重复扫描暂不可新开批次。" + last_error)
                self.stop.wait(self.poll_seconds)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def close(self):
        self.stop.set()


class ScanHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store, *, coordinator=None, monitor=None, namer=None, static_root=HERE):
        self.store = store
        self.coordinator = coordinator or ScanCoordinator(store)
        self.monitor = monitor
        self.namer = namer
        self.static_root = Path(static_root)
        super().__init__(address, Handler)

    def server_close(self):
        self.coordinator.close()
        if self.monitor is not None:
            self.monitor.close()
        if self.namer is not None:
            self.namer.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = "ScanStation/2.0"

    def log_message(self, format, *args):
        LOG.info("%s - %s", self.address_string(), format % args)

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, value):
        self._send(code, json.dumps(value, ensure_ascii=False), "application/json; charset=utf-8")

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("无效请求长度") from None
        if length < 0 or length > 8192:
            raise ValueError("请求过大")
        try:
            body = json.loads(self.rfile.read(length)) if length else {}
        except (ValueError, UnicodeDecodeError):
            raise ValueError("请求须为有效 JSON") from None
        if not isinstance(body, dict):
            raise ValueError("请求须为 JSON 对象")
        return body

    def do_GET(self):
        try:
            self._get()
        except DocumentConflict as exc:
            self._json(409, {"error": str(exc)})
        except (KeyError, FileNotFoundError):
            self._json(404, {"error": "文档或页面不存在"})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception:
            LOG.exception("GET failed")
            self._json(500, {"error": "读取失败，请稍后重试"})

    def _get(self):
        path = urlparse(self.path).path
        parts = [part for part in path.split("/") if part]
        static = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/index.html": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if path in static:
            filename, content_type = static[path]
            self._send(200, (self.server.static_root / filename).read_bytes(), content_type)
            return
        if path == "/documents":
            self._json(200, {"documents": self.server.store.list(),
                             "active_id": self.server.coordinator.active_id})
            return
        if path == "/scanner/status":
            snapshot = self.server.monitor.snapshot() if self.server.monitor is not None else {
                "state": "unknown", "message": "尚未收到设备状态", "checked_at": None,
                "supported_dpi": []}
            if self.server.coordinator.active_id and snapshot.get("state") == "online":
                snapshot = dict(snapshot, state="scanning", message="正在处理扫描任务")
            self._json(200, snapshot)
            return
        if len(parts) >= 3 and parts[0] == "scan":
            document_id, action = parts[1:3]
            self.server.store.get(document_id)
            if len(parts) == 3 and action == "status":
                self._json(200, self.server.store.get(document_id))
                return
            if len(parts) == 4 and action in ("page", "original"):
                if not re.fullmatch(r"[1-9][0-9]*", parts[3]):
                    raise ValueError("无效页码")
                if action == "original":
                    data = self.server.store.page_bytes(document_id, int(parts[3]), original=True)
                else:
                    query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                    if any(len(query[key]) != 1 for key in ("mode", "v", "ev") if key in query):
                        raise ValueError("图片参数不能重复")
                    versions = {}
                    for key, argument in (("v", "revision"), ("ev", "render_version")):
                        if key in query:
                            if not re.fullmatch(r"[1-9][0-9]*", query[key][0]):
                                raise ValueError("无效图片版本")
                            versions[argument] = int(query[key][0])
                    data = self.server.store.rendered_page_bytes(document_id, int(parts[3]),
                        mode=query.get("mode", [None])[0], **versions)
                self._send(200, data, "image/jpeg")
                return
            if len(parts) == 3 and action == "pdf":
                data, name = self.server.store.pdf(document_id)
                safe_name = name.replace("/", "_").replace("\\", "_")
                if not safe_name.lower().endswith(".pdf"):
                    safe_name += ".pdf"
                self._send(200, data, "application/pdf", {"Content-Disposition":
                    f"attachment; filename=\"scan-{document_id}.pdf\"; filename*=UTF-8''{quote(safe_name, safe='')}"})
                return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        try:
            self._post()
        except ScanBusy as exc:
            self._json(409, {"error": str(exc), "active_id": exc.active_id})
        except DocumentConflict as exc:
            self._json(409, {"error": str(exc)})
        except (KeyError, FileNotFoundError):
            self._json(404, {"error": "文档或页面不存在"})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception:
            LOG.exception("POST failed")
            self._json(500, {"error": "操作失败，请稍后重试"})

    def _post(self):
        path = urlparse(self.path).path
        parts = [part for part in path.split("/") if part]
        body = self._body()
        if path == "/scan":
            self._check_options(body)
            result = self.server.coordinator.start(name=body.get("name"), dpi=body.get("dpi", 150),
                                                   duplex=body.get("duplex", False), auto_name=body.get("auto_name"),
                                                   render_mode=body.get("render_mode", "original"))
            self._json(202, result)
            return
        if len(parts) >= 3 and parts[0] == "scan":
            document_id, action = parts[1:3]
            if len(parts) == 3 and action == "set-render-mode":
                if set(body) != {"render_mode", "updated_at"}:
                    raise ValueError("请提供图片模式和当前文件的更新时间")
                self._json(200, self.server.store.set_render_mode(document_id, body["render_mode"],
                           updated_at=body["updated_at"]))
                return
            if len(parts) == 3 and action == "abandon":
                if body:
                    raise ValueError("强制终止本批不接受额外参数")
                self._json(200, self.server.coordinator.abandon(document_id))
                return
            if len(parts) == 3 and action == "delete-document":
                if set(body) != {"updated_at"}:
                    raise ValueError("请提供当前文件的更新时间")
                self._json(200, self.server.coordinator.delete_document(
                    document_id, updated_at=body["updated_at"]))
                return
            if len(parts) == 3 and action == "continue":
                self._check_options(body)
                result = self.server.coordinator.start(document_id=document_id, name=body.get("name"),
                            dpi=body.get("dpi", 150), duplex=body.get("duplex", False))
                self._json(202, result)
                return
            if len(parts) == 3 and action == "rename":
                self._json(200, self.server.store.rename(document_id, body.get("name")))
                return
            if len(parts) == 3 and action == "name-lock":
                self._json(200, self.server.store.lock_name(document_id))
                return
            if len(parts) == 3 and action == "reorder":
                document = self.server.store.reorder(document_id, body.get("pages"), body.get("order_revision"))
                self._json(200, document)
                return
            if len(parts) == 3 and action == "blank-analysis":
                if body:
                    raise ValueError("空白页分析不接受额外参数")
                self._json(200, self.server.store.analyze_blank_pages(document_id))
                return
            if len(parts) == 3 and action == "delete-selected":
                if set(body) != {"pages", "order_revision", "page_revisions"}:
                    raise ValueError("请选择页面并提供完整页面版本")
                result = self.server.store.delete_selected(document_id, body["pages"],
                    order_revision=body["order_revision"], page_revisions=body["page_revisions"])
                self._json(200, result)
                return
            if len(parts) == 3 and action == "blank-undo":
                if set(body) != {"cleanup_id"}:
                    raise ValueError("请提供删除记录")
                self._json(200, self.server.store.undo_blank_cleanup(document_id, body["cleanup_id"]))
                return
            if len(parts) == 4 and action == "rescan":
                if not re.fullmatch(r"[1-9][0-9]*", parts[3]):
                    raise ValueError("无效页码")
                number = int(parts[3])
                document = self.server.store.get(document_id)
                if number not in document["pages"]:
                    raise KeyError(number)
                snapshot = self.server.monitor.snapshot() if self.server.monitor is not None else {}
                if snapshot.get("supports_page_rescan") is not True:
                    raise ValueError("扫描端尚未确认支持单页重扫")
                self._check_options({"dpi": document["page_details"][str(number)]["dpi"], "duplex": False})
                self._json(202, self.server.coordinator.rescan(document_id, number))
                return
            if len(parts) == 4 and action == "delete":
                if not re.fullmatch(r"[1-9][0-9]*", parts[3]):
                    raise ValueError("无效页码")
                document = self.server.store.delete_page(document_id, int(parts[3]))
                self._json(200, {"ok": True, "pages": document["pages"], "document": document})
                return
        self._json(404, {"error": "not found"})

    def _check_options(self, body):
        if "render_mode" in body:
            validate_render_mode(body["render_mode"])
        if "auto_name" in body and type(body["auto_name"]) is not bool:
            raise ValueError("auto_name 必须是布尔值")
        options = scan_options(body.get("dpi", 150), body.get("duplex", False))
        snapshot = self.server.monitor.snapshot() if self.server.monitor is not None else {}
        # Refuse before reserving the device. A batch whose scanner computer
        # never answers is never released, so an operator must not be able to
        # start one while that computer or its agent is unreachable.
        problem = readiness_problem(snapshot)
        if problem is not None:
            raise ValueError(problem)
        supported = snapshot.get("supported_dpi", [])
        if (supported and options["dpi"] not in supported) or (options["dpi"] != 150 and not supported):
            raise ValueError("尚未确认扫描仪支持该 DPI，请选择已支持的分辨率")
        if options["duplex"] and snapshot.get("duplex_supported") is not True:
            raise ValueError("尚未确认扫描仪支持自动双面，请选择单面扫描")


def main():
    from scanner_status import ScannerMonitor
    from ocr_naming import OCRNamingWorker
    logging.basicConfig(level=logging.INFO)
    store = DocumentStore(DATA_ROOT, legacy_root=LEGACY_ROOT)
    coordinator = ScanCoordinator(store)
    monitor = ScannerMonitor(read_scanner_status)
    namer = OCRNamingWorker(store)
    store.on_page_published = namer.submit
    server = ScanHTTPServer((LISTEN_HOST, configured_port()),
                            store, coordinator=coordinator, monitor=monitor, namer=namer)
    monitor.start()
    namer.start()
    namer.recover()
    coordinator.recover()
    # A force-end performed while the scanner computer was unreachable leaves
    # its request queued for a retry, including across a service restart.
    coordinator.start_withdrawal_sweeper()
    LOG.info("Scan Station on %s:%s; history: %s", *server.server_address, DATA_ROOT)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
