"""Offline image-only crop report; never opens a scanner or network connection."""
import argparse
import io
import json
from pathlib import Path
import re
import sys
import time

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from image_processing import process_page


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--pattern', default='*.jpg')
    args = parser.parse_args()
    files = sorted(args.source.glob(args.pattern), key=lambda p: int(re.search(r'(\d+)\.jpg$', p.name)[1]))
    args.output.mkdir(parents=True, exist_ok=True)
    records = []
    sheet = Image.new('RGB', (1320, max(1, (len(files) + 5) // 6) * 335), '#d8e0e8')
    draw = ImageDraw.Draw(sheet)
    for index, path in enumerate(files):
        raw = path.read_bytes()
        start = time.monotonic()
        result, meta = process_page(raw)
        elapsed = time.monotonic() - start
        (args.output / path.name).write_bytes(result)
        im = Image.open(io.BytesIO(result))
        gray = np.asarray(im).mean(axis=2)
        bands = (gray[:, :5], gray[:5], gray[:, -5:], gray[-5:])
        record = {'file': path.name, 'seconds': round(elapsed, 4), **meta,
                  'edge_dark_fraction': [round(float((band < 100).mean()), 6) for band in bands]}
        if not meta.get('perspective_corrected') and meta.get('edge_crop'):
            expected = np.asarray(Image.open(io.BytesIO(raw)).convert('RGB').crop(meta['edge_crop']))
            actual = np.asarray(im)
            # Ignore only the outer 60 pixels where background cleanup occurs.
            diff = np.abs(expected[60:-60, 60:-60].astype(float) - actual[60:-60, 60:-60])
            record['interior_mean_absolute_difference'] = round(float(diff.mean()), 4)
        records.append(record)
        im.thumbnail((210, 303))
        x, y = index % 6 * 220 + 5, index // 6 * 335 + 22
        sheet.paste(im, (x, y))
        draw.text((x, y - 17), path.name, fill='black')
    sheet.save(args.output / 'contact.jpg')
    report = {'pages': len(records), 'cropped': sum(r['cropped'] for r in records),
              'total_seconds': round(sum(r['seconds'] for r in records), 3), 'records': records}
    (args.output / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != 'records'}))


if __name__ == '__main__':
    main()
