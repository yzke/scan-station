"""Run the real web backend with a strictly in-memory scanner for UI checks."""
import argparse
import io
import json
from pathlib import Path
import sys
import time

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from documents import DocumentStore
from ocr_naming import OCRNamingWorker
from server import ScanCoordinator, ScanHTTPServer


class Entry:
    def __init__(self, name):
        self.name = name

    def get_longname(self):
        return self.name


class ImageQueue:
    """Implements the SMB methods in memory: no sockets and no Windows writes."""
    def __init__(self, images):
        self.images = images
        self.batches = {}
        self.uploads = {}

    def putFile(self, share, path, callback):
        data = bytearray()
        while chunk := callback(65536):
            data.extend(chunk)
        self.uploads[path] = bytes(data)

    def rename(self, share, source, destination):
        json.loads(self.uploads.pop(source))
        self.batches[Path(destination).stem] = time.monotonic()

    def listPath(self, share, path):
        names = []
        for batch, started in self.batches.items():
            age = time.monotonic() - started
            for number in range(1, min(int(age / 1.3), len(self.images)) + 1):
                names.append(f'{batch}-p{number}.jpg')
            if age > (len(self.images) + 1) * 1.3:
                names.append(f'{batch}.status')
        return [Entry(name) for name in names]

    def getFile(self, share, path, callback):
        name = Path(path).name
        if name.endswith('.status'):
            callback(f'ok:{len(self.images)}'.encode())
        else:
            number = int(name.rsplit('-p', 1)[1].split('.')[0])
            callback(self.images[number - 1])

    def close(self):
        pass


class Monitor:
    def snapshot(self):
        return {'state': 'online', 'message': '图片模拟设备', 'name': '图片模拟设备',
                'checked_at': time.time(), 'supported_dpi': [150, 200, 300],
                'duplex_supported': True}

    def close(self):
        pass


def synthetic_pages():
    pages = []
    for number in (1, 2):
        image = Image.new('RGB', (600, 850), '#ececec')
        draw = ImageDraw.Draw(image)
        draw.rectangle((24, 24, 576, 826), fill='white', outline='#b0b0b0')
        draw.text((55, 65), f'Synthetic demo document - page {number}', fill='black')
        for row in range(10):
            top = 125 + row * 45
            draw.line((55, top + 25, 540, top + 25), fill='#888888', width=1)
            draw.text((65, top), f'Demo row {row + 1}: generated sample content', fill='#333333')
        draw.ellipse((430, 660, 530, 760), outline='#bd3535', width=4)
        output = io.BytesIO()
        image.save(output, 'JPEG', quality=95, dpi=(300, 300))
        pages.append(output.getvalue())
    return pages


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18081)
    parser.add_argument('--data', default='output/integration-documents')
    args = parser.parse_args()
    images = synthetic_pages()
    store = DocumentStore(args.data)
    queue = ImageQueue(images)
    coordinator = ScanCoordinator(store, connection_factory=lambda: queue, poll_seconds=.2)
    namer = OCRNamingWorker(store, recognizer=lambda path: 'Synthetic demo document')
    store.on_page_published = namer.submit
    namer.start()
    namer.recover()
    server = ScanHTTPServer(('127.0.0.1', args.port), store, coordinator=coordinator, monitor=Monitor())
    print(f'Image-only demo http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    finally:
        namer.close()
        server.server_close()


if __name__ == '__main__':
    main()
