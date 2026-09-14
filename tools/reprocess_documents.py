#!/usr/bin/env python3
"""Reprocess saved pages offline; never connects to SMB or starts a scan."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from documents import DocumentConflict, DocumentStore
from image_processing import PROCESSING_VERSION


def main():
    parser = argparse.ArgumentParser(description="停用网页服务后，从当前原图离线重处理历史页面")
    parser.add_argument("--data-dir", required=True, type=Path, help="持久化 documents 目录")
    parser.add_argument("--document-id", help="只处理此文档；省略则检查全部历史")
    parser.add_argument("--dry-run", action="store_true", help="列出变更计划，不写入历史或生成图片")
    args = parser.parse_args()
    try:
        # Loading must not perform normal startup recovery, including during a
        # dry run. Selected pages are processed only by the explicit operation.
        store = DocumentStore(args.data_dir, recover=False)
        pages = store.reprocess_pages(args.document_id, dry_run=args.dry_run,
                                      processing_version=PROCESSING_VERSION)
    except (DocumentConflict, KeyError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    result = {"dry_run": args.dry_run, "processing_version": PROCESSING_VERSION,
              "pages": pages, "counts": {action: sum(p["action"] == action for p in pages)
                                           for action in ("reprocessed", "would_reprocess", "skip", "failed")}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["counts"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
