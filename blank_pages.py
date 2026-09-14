"""Conservative, image-only blank-page suggestions. No pixels are changed."""
import cv2
import numpy as np
from PIL import Image

from image_processing import decode_jpeg

ANALYSIS_VERSION = 1


def analyze_page(data):
    """Suggest a blank page; users review checkboxes before deleting anything."""
    image = decode_jpeg(data)
    image.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
    pixels = np.asarray(image)
    gray = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
    height, width = gray.shape
    paper_level = float(np.median(gray))
    metrics = {"paper_level": round(paper_level, 2), "size": [width, height]}

    def result(blank, reason):
        return {"is_blank": bool(blank), "version": ANALYSIS_VERSION,
                "reason": reason, "metrics": metrics}

    if min(width, height) < 100 or paper_level < 135:
        return result(False, "图像过小或纸面过暗，保留人工判断")

    # Smooth JPEG/sensor noise, while closing estimates the local paper shade
    # around printed strokes. A global dark mask also preserves large filled
    # areas which closing alone cannot distinguish from their own background.
    smooth_rgb = cv2.GaussianBlur(pixels, (3, 3), .6)
    smooth = cv2.cvtColor(smooth_rgb, cv2.COLOR_RGB2GRAY)
    background = cv2.morphologyEx(smooth, cv2.MORPH_CLOSE, np.ones((31, 31), np.uint8))
    noise = float(np.median(np.abs(gray.astype(np.float32) - cv2.medianBlur(gray, 3)))) * 1.4826
    # Keep pale small print after reduction from a 300 DPI scan. Seven levels
    # still separate the very weak paper/show-through samples used for review.
    threshold = max(7.0, min(16.0, noise * 3))
    contrast = background.astype(np.int16) - smooth.astype(np.int16)
    foreground = (contrast >= threshold) | (smooth < paper_level - 42)

    # Faint colored stamps can have almost the same luminance as the paper.
    # Relative chroma retains these without treating uniform blue paper as ink.
    rgb = smooth_rgb.astype(np.int16)
    red_green, blue_green = rgb[:, :, 0] - rgb[:, :, 1], rgb[:, :, 2] - rgb[:, :, 1]
    chroma = np.maximum(np.abs(red_green - np.median(red_green)),
                        np.abs(blue_green - np.median(blue_green)))
    foreground |= chroma >= 10

    # Closing alone misses broad filled areas. Their boundaries remain visible
    # at this scale, while smoothing suppresses individual sensor-noise pixels.
    # Record true edge contact before this wider mask grows into the margins.
    left, right = np.mean(foreground[:, 0]) >= .7, np.mean(foreground[:, -1]) >= .7
    top, bottom = np.mean(foreground[0, :]) >= .7, np.mean(foreground[-1, :]) >= .7
    shaded = cv2.GaussianBlur(smooth, (0, 0), 2)
    edges = cv2.morphologyEx(shaded, cv2.MORPH_GRADIENT, np.ones((9, 9), np.uint8))
    foreground |= edges >= threshold

    count, _, stats, _ = cv2.connectedComponentsWithStats(foreground.astype(np.uint8), connectivity=8)
    areas = []
    significant = 0
    # Blur plus the gradient mask extends a proven residual strip by up to ten
    # pixels. Near-margin printing must not become residue through this growth.
    border_x, border_y = max(3, round(width * .006)) + 10, max(3, round(height * .006)) + 10
    for x, y, w, h, area in stats[1:]:
        # A very thin strip flush against an image edge is scanner residue.
        # A printed frame or a local margin mark does not meet this geometry.
        edge_strip = ((((x == 0 and left) or (x + w == width and right)) and w <= border_x and h >= height * .7)
                      or (((y == 0 and top) or (y + h == height and bottom)) and h <= border_y and w >= width * .7))
        if edge_strip or area < 4:
            continue
        areas.append(int(area))
        if area >= 8 or (area >= 6 and max(w, h) >= 8):
            significant += 1
    ink_pixels = sum(areas)
    metrics.update(noise=round(noise, 2), contrast_threshold=round(threshold, 2),
                   components=len(areas), significant_components=significant,
                   foreground_pixels=ink_pixels, largest_component=max(areas, default=0))
    if significant or ink_pixels >= 32:
        return result(False, "检测到文字、线条或印记")
    return result(True, "未检测到明显内容，请核对后删除")
