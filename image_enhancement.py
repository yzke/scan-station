"""Deterministic document renderings; see docs/image-enhancement.md for sources.

The input has already passed paper-edge correction. These operations never
change pixel geometry, recognize text, or overwrite the original JPEG.
"""
import io

import cv2
import numpy as np
from PIL import Image

ENHANCEMENT_VERSION = 1
ENHANCEMENT_MODES = ('original', 'enhanced', 'bw')
_MAX_PIXELS = 40_000_000
_STRIP_ROWS = 256

# Lift only the top three gray levels to white and gently deepen midtones.
# A small highlight shoulder removes residual paper grain without a hard
# document-wide threshold that would discard faint print or colored ink.
_TONE_CURVE = np.rint(
    255 * np.clip((np.arange(256, dtype=np.float32) - 4) / 248, 0, 1) ** 1.15
).astype(np.uint8)


def _paper_background(rgb):
    """Estimate slowly varying RGB paper color on a reduced working image."""
    height, width = rgb.shape[:2]
    scale = min(1.0, 1200 / max(height, width))
    small = cv2.resize(rgb, (max(1, round(width * scale)),
                             max(1, round(height * scale))),
                       interpolation=cv2.INTER_AREA)
    window = max(3, round(min(small.shape[:2]) * .04)) | 1
    kernel = np.ones((window, window), dtype=np.uint8)
    # Closing fills dark strokes in the *background estimate*, not the page.
    background = cv2.morphologyEx(small, cv2.MORPH_CLOSE, kernel,
                                  borderType=cv2.BORDER_REPLICATE)
    background = cv2.GaussianBlur(background, (0, 0), max(1, window / 4),
                                   borderType=cv2.BORDER_REPLICATE)
    paper = np.percentile(background.reshape(-1, 3), 80, axis=0)
    # A large logo, photo, or filled text area can exceed the closing window.
    # Bound its estimated paper gain so it cannot normalize itself to white.
    floor = np.maximum(96, paper * .78).astype(np.uint8)
    background = np.maximum(background, floor)
    return cv2.resize(background, (width, height), interpolation=cv2.INTER_LINEAR)


def _normalize_paper(rgb):
    background = _paper_background(rgb)
    result = np.empty_like(rgb)
    for start in range(0, rgb.shape[0], _STRIP_ROWS):
        end = start + _STRIP_ROWS
        normalized = rgb[start:end].astype(np.float32) * 255 / background[start:end]
        levels = np.rint(np.clip(normalized, 0, 255)).astype(np.uint8)
        result[start:end] = cv2.LUT(levels, _TONE_CURVE)
    return result


def _black_and_white(rgb):
    # A small darkest-channel contribution retains pale colored marks while
    # luminance averaging suppresses the scanner's chromatic paper noise.
    gray = cv2.addWeighted(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), .75,
                           rgb.min(axis=2), .25, 0)
    # Unlike median filtering or morphological cleanup of the binary result,
    # this suppresses paper grain without deleting isolated punctuation.
    gray = cv2.bilateralFilter(gray, 5, 15, 1.3)
    window = max(25, min(101, round(min(gray.shape) / 45))) | 1
    radius = window // 2
    result = np.empty_like(gray)
    for start in range(0, gray.shape[0], _STRIP_ROWS):
        end = min(gray.shape[0], start + _STRIP_ROWS)
        top, bottom = max(0, start - radius), min(gray.shape[0], end + radius)
        local = gray[top:bottom]
        mean = cv2.boxFilter(local, cv2.CV_32F, (window, window),
                             borderType=cv2.BORDER_REPLICATE)
        square_mean = cv2.sqrBoxFilter(local, cv2.CV_32F, (window, window),
                                       borderType=cv2.BORDER_REPLICATE)
        deviation = np.sqrt(np.maximum(square_mean - mean * mean, 0))
        # Sauvola (R=128, k=.15), with a local mean threshold to retain weak
        # strokes in otherwise clean, low-contrast neighborhoods.
        sauvola = mean * (1 + .15 * (deviation / 128 - 1))
        weak_ink = mean - np.maximum(14, .22 * deviation)
        threshold = np.maximum(sauvola, weak_ink)[start - top:end - top]
        section = gray[start:end]
        # Keep interiors of dark filled letters/logos wider than the window.
        foreground = (section < 180) | (section <= threshold)
        result[start:end] = np.where(foreground, 0, 255)
    return result


def enhance_jpeg(data: bytes, mode: str) -> bytes:
    """Render an already corrected JPEG as original, enhanced, or bw.

    ``original`` returns the exact input bytes without decoding. Derived
    renderings preserve pixel dimensions and EXIF orientation rather than
    rotating/resampling the page; existing EXIF and DPI data are retained.
    Black-and-white output is grayscale JPEG (its compression can introduce
    small gray values immediately next to binary edges).
    """
    if mode not in ENHANCEMENT_MODES:
        raise ValueError('图片模式必须是 original、enhanced 或 bw')
    if mode == 'original':
        return data
    if not data.startswith(b'\xff\xd8') or not data.rstrip().endswith(b'\xff\xd9'):
        raise ValueError('JPEG 尚未写入完成')
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.format != 'JPEG':
                raise ValueError('图片必须为 JPEG')
            if source.width * source.height > _MAX_PIXELS:
                raise ValueError('扫描图片尺寸过大')
            source.load()
            metadata = {key: source.info[key] for key in ('dpi', 'exif') if key in source.info}
            if mode == 'enhanced' and source.mode == 'RGB' and source.info.get('icc_profile'):
                metadata['icc_profile'] = source.info['icc_profile']
            rgb = np.asarray(source.convert('RGB'))
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError('扫描图片无法解码') from error
    normalized = _normalize_paper(rgb)
    result = Image.fromarray(_black_and_white(normalized) if mode == 'bw' else normalized)
    output = io.BytesIO()
    result.save(output, 'JPEG', quality=96, subsampling=0, **metadata)
    return output.getvalue()
