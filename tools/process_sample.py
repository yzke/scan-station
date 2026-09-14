"""Validate correction on a supplied PDF without contacting the scanner."""
import argparse
import io
import json
from pathlib import Path
import sys

import fitz
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from image_processing import process_page


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('pdf')
    parser.add_argument('output')
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    source = fitz.open(args.pdf)
    result = fitz.open()
    sheet = Image.new('RGB', (6 * 220, ((len(source) + 5) // 6) * 335), '#e8ebef')
    draw = ImageDraw.Draw(sheet)
    records = []
    for number, page in enumerate(source, 1):
        images = page.get_images()
        if len(images) != 1:
            raise ValueError('Expected a single scanned image per page')
        raw = source.extract_image(images[0][0])['image']
        (output / f'raw-{number}.jpg').write_bytes(raw)
        corrected, metadata = process_page(raw)
        (output / f'corrected-{number}.jpg').write_bytes(corrected)
        records.append({'page': number, **metadata})
        image = Image.open(io.BytesIO(corrected))
        w, h = image.size
        result.new_page(width=w / 150 * 72, height=h / 150 * 72).insert_image(
            fitz.Rect(0, 0, w / 150 * 72, h / 150 * 72), stream=corrected)
        image.thumbnail((210, 303))
        x = ((number - 1) % 6) * 220 + 5
        y = ((number - 1) // 6) * 335 + 22
        sheet.paste(image, (x, y))
        draw.text((x, y - 17), str(number), fill='black')
    result.save(output / 'corrected-pages.pdf')
    sheet.save(output / 'contact-corrected.jpg')
    (output / 'crop-results.json').write_text(json.dumps(records, ensure_ascii=False, indent=2))
    print(json.dumps({'pages': len(records), 'cropped': sum(r['cropped'] for r in records),
                      'dimensions': [r['size'] for r in records]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
