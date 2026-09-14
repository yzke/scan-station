"""Linux-only first-line OCR and a single, nonblocking document naming worker."""
from __future__ import annotations

import csv
import io
import logging
import os
import queue
import re
import subprocess
import threading
import unicodedata

from PIL import Image, ImageOps

LOG = logging.getLogger("scan-station.ocr")


def clean_filename(text):
    """Make OCR output a short portable filename, without modifying image data."""
    if not isinstance(text, str):
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    text = re.sub(r"(?<=[\u3400-\u9fff]) (?=[\u3400-\u9fff])", "", text)
    text = text[:100].rstrip(" .")
    if not any(char.isalnum() for char in text):
        return ""
    if re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", text):
        text = "_" + text
    return text


def first_line_from_tsv(tsv):
    """Use the first text line, or fail; later body text must not replace it."""
    if isinstance(tsv, bytes):
        tsv = tsv.decode("utf-8", "replace")
    lines = {}
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        try:
            word = row.get("text", "").strip()
            confidence = float(row["conf"])
            if int(row["level"]) != 5 or not word or confidence < 0:
                continue
            key = tuple(row[k] for k in ("page_num", "block_num", "par_num", "line_num"))
            lines.setdefault(key, []).append((int(row["top"]), int(row["left"]), word, confidence))
        except (KeyError, ValueError, TypeError):
            continue
    candidates = sorted(lines.values(), key=lambda words: (min(w[0] for w in words), min(w[1] for w in words)))
    for words in candidates:
        words.sort(key=lambda word: word[1])
        text = clean_filename(" ".join(word[2] for word in words))
        weight = sum(len(word[2]) for word in words)
        confidence = sum(len(word[2]) * word[3] for word in words) / max(1, weight)
        count = sum(char.isalnum() for char in text)
        if not count:
            continue
        return text if count >= 2 and confidence >= 35 else ""
    return ""


def recognize_first_line(image_path, *, timeout=10, runner=subprocess.run):
    """OCR the corrected first page's top region; no temporary image is persisted."""
    with Image.open(image_path) as image:
        image.load()
        gray = ImageOps.grayscale(image)
        width, height = gray.size
        region = gray.crop((0, 0, width, max(1, round(height * 0.35))))
        if width > 2400:
            region = region.resize((2400, max(1, round(region.height * 2400 / width))))
        elif width < 1200:
            factor = min(3, 1200 / width)
            region = region.resize((round(width * factor), max(1, round(region.height * factor))))
        output = io.BytesIO()
        region.save(output, "PNG")
    environment = dict(os.environ, OMP_THREAD_LIMIT="1", OMP_NUM_THREADS="1")
    result = runner(["tesseract", "stdin", "stdout", "-l", "chi_sim+eng", "--psm", "6", "tsv"],
                    input=output.getvalue(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=timeout, check=True, env=environment)
    return first_line_from_tsv(result.stdout)


class OCRNamingWorker:
    """One daemon consumes a queue; scan collection only enqueues document IDs."""
    def __init__(self, store, *, recognizer=recognize_first_line):
        self.store = store
        self.recognizer = recognizer
        self.queue = queue.Queue()
        self.lock = threading.Lock()
        self.queued = set()
        self.stopped = threading.Event()
        self.thread = None

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="scan-ocr-naming", daemon=True)
            self.thread.start()

    def submit(self, document_id, page_number=1):
        if page_number != 1 or self.stopped.is_set() or not self.store.ocr_eligible(document_id):
            return
        with self.lock:
            if document_id not in self.queued:
                self.queued.add(document_id)
                self.queue.put(document_id)

    def recover(self):
        for document_id in self.store.pending_ocr():
            self.submit(document_id)

    def _run(self):
        while not self.stopped.is_set():
            document_id = self.queue.get()
            if document_id is None:
                self.queue.task_done()
                return
            try:
                path = self.store.begin_ocr(document_id)
                if path is not None:
                    try:
                        text = self.recognizer(path)
                    except Exception as exc:
                        LOG.info("OCR naming failed for %s: %s", document_id, type(exc).__name__)
                        self.store.finish_ocr(document_id, "", error=type(exc).__name__)
                    else:
                        self.store.finish_ocr(document_id, text)
            except Exception:
                LOG.exception("Cannot update OCR naming for %s", document_id)
            finally:
                with self.lock:
                    self.queued.discard(document_id)
                self.queue.task_done()

    def close(self):
        self.stopped.set()
        self.queue.put(None)
