"""Conservative paper-edge correction, executed on the Linux station only."""
import io

import cv2
import numpy as np
from PIL import Image

PROCESSING_VERSION = 2


def decode_jpeg(data):
    """Reject a file still being written by the Windows scan agent."""
    if not data.startswith(b'\xff\xd8') or not data.rstrip().endswith(b'\xff\xd9'):
        raise ValueError('JPEG 尚未写入完成')
    with Image.open(io.BytesIO(data)) as probe:
        if probe.width * probe.height > 40_000_000:
            raise ValueError('扫描图片尺寸过大')
        probe.verify()
    with Image.open(io.BytesIO(data)) as source:
        source.load()
        return source.convert('RGB')


def _paper_corners(image):
    """Locate a large, bright sheet surrounded by darker feeder background.

    Opening removes the feeder's narrow white alignment stripe. Geometric
    and contrast checks deliberately prefer leaving an uncertain page alone.
    """
    w, h = image.size
    scale = min(1.0, 1000 / max(w, h))
    small = image.resize((max(1, round(w * scale)), max(1, round(h * scale))))
    gray = cv2.cvtColor(np.asarray(small), cv2.COLOR_RGB2GRAY)
    if min(gray.shape) < 60:
        return None
    mask = np.uint8(gray > 95) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    fraction = cv2.contourArea(contour) / gray.size
    if not .35 <= fraction <= .995:
        return None
    perimeter = cv2.arcLength(contour, True)
    polygon = cv2.approxPolyDP(contour, .015 * perimeter, True)
    if len(polygon) != 4 or not cv2.isContourConvex(polygon):
        return None
    points = polygon.reshape(4, 2).astype(np.float32)
    # Sort clockwise from top-left without duplicate indices on diamond shapes.
    center = points.mean(axis=0)
    points = points[np.argsort(np.arctan2(points[:, 1] - center[1], points[:, 0] - center[0]))]
    points = np.roll(points, -np.argmin(points.sum(axis=1)), axis=0)
    edges = np.roll(points, -1, axis=0) - points
    lengths = np.linalg.norm(edges, axis=1)
    if min(lengths) < min(gray.shape) * .35:
        return None
    cosines = np.abs(np.sum(edges * np.roll(edges, -1, axis=0), axis=1)
                     / (lengths * np.roll(lengths, -1)))
    if max(cosines) > .3:
        return None
    inside = np.zeros_like(gray)
    cv2.fillConvexPoly(inside, points.astype(np.int32), 255)
    # Exclude an edge band: paper shadows and antialiasing are not background.
    outside = cv2.dilate(inside, np.ones((7, 7), np.uint8)) == 0
    core = cv2.erode(inside, np.ones((7, 7), np.uint8)) > 0
    if outside.sum() < gray.size * .008 or not core.any():
        return None
    if np.median(gray[core]) - np.median(gray[outside]) < 55:
        return None
    if np.mean(gray[outside] < 100) < .70:
        return None
    # Independent x/y scaling avoids rounding shifts in the reduced image.
    points *= np.array([w / gray.shape[1], h / gray.shape[0]], np.float32)
    return points


def _edge_profile(gray):
    """Track the transition from an edge-connected dark band into paper.

    The first axis runs along an edge, the second goes into the page. A
    robust smooth profile ignores local notches caused by ink touching paper.
    """
    length, depth = gray.shape
    run = max(9, min(17, round(length / 200)))
    light = np.uint8(gray >= 115)
    starts = cv2.erode(light, np.ones((1, run), np.uint8), anchor=(0, 0),
                       borderType=cv2.BORDER_CONSTANT, borderValue=0)
    has_paper = starts.any(axis=1)
    positions = starts.argmax(axis=1).astype(np.float64)
    valid = has_paper & (positions < depth - run)
    if valid.mean() < .75:
        return None
    coordinates = np.arange(length)
    # Most bands are straight or very slightly curved. Bin medians retain
    # that shape without following a stamp/handwriting into the document.
    knots, values = [], []
    for indices in np.array_split(coordinates, min(64, max(8, length // 25))):
        usable = indices[valid[indices]]
        if len(usable) >= max(2, len(indices) // 2):
            knots.append(float(np.median(usable)))
            values.append(float(np.median(positions[usable])))
    if len(knots) < 6:
        return None
    smooth = np.interp(coordinates, knots, values)
    tolerance = max(2.0, float(np.median(smooth)) * .12)
    coherent = valid & (np.abs(positions - smooth) <= tolerance)
    if coherent.mean() < .82:
        return None
    # Keep exact edge locations on consistent rows; never follow a deep
    # local dark mark inward. Small positive differences are antialiasing.
    boundary = np.where(coherent, np.minimum(positions, smooth + 1), smooth)
    boundary = np.maximum(0, np.floor(boundary)).astype(np.int32)
    positive = boundary > 0
    if positive.mean() < .15:
        return None
    dark = (gray < 115).cumsum(axis=1)
    outer_fraction = dark[np.arange(length), np.maximum(0, boundary - 1)] / np.maximum(1, boundary)
    if np.mean(outer_fraction[positive] >= .7) < .9:
        return None
    # A scanner edge has a few mixed dark/paper pixels. Refine only
    # after validating the dark band, relative to the nearby paper tone.
    # Never follow a local deep ink mark inward.
    along = np.arange(length)
    sample_at = np.minimum(boundary[:, None] + np.arange(5, 18), depth - 1)
    paper_level = np.median(gray[along[:, None], sample_at], axis=1)
    for _ in range(4):
        at = np.minimum(boundary, depth - 1)
        boundary += coherent & (gray[along, at] < paper_level - 22)
    return boundary


def _remove_external_bands(image, *, paper_confirmed=False):
    """Remove only coherent background connected to the outside of the sheet.

    Crop the band common to every row/column, and fill its small curved
    remainder with the neighboring paper color. Content inside the fitted
    sheet is never flood-filled, so a printed frame is not treated as a hole.
    """
    pixels = np.asarray(image).copy()
    gray = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    if min(w, h) < 100:
        return image, None
    widths = (min(w // 5, max(32, round(w * .07))),
              min(h // 5, max(32, round(h * .07))))
    sides = [gray[:, :widths[0]], gray[:, ::-1][:, :widths[0]],
             gray.T[:, :widths[1]], gray[::-1, :].T[:, :widths[1]]]
    profiles = [_edge_profile(side) for side in sides]
    # A thin printed frame at the image edge is ambiguous. Require either
    # prior whole-paper detection, or at least one clear overscan band.
    strong = any(profile is not None and np.quantile(profile, .6) >= max(5, extent * .009)
                 for profile, extent in zip(profiles, (w, w, h, h)))
    if not paper_confirmed and not strong:
        return image, None
    bounds = [0, 0, 0, 0]
    changed = 0
    for side, profile in enumerate(profiles):
        if profile is None:
            continue
        # Only crop the strip outside the paper on every row/column.
        bounds[side] = int(profile.min())
        view = (pixels if side == 0 else pixels[:, ::-1] if side == 1
                else pixels.transpose(1, 0, 2) if side == 2
                else pixels[::-1].transpose(1, 0, 2))
        along = np.arange(len(profile))
        samples = np.minimum(profile[:, None] + np.arange(5, 30)[None, :], view.shape[1] - 1)
        swatches = view[along[:, None], samples]
        bright = cv2.cvtColor(swatches, cv2.COLOR_RGB2GRAY) >= 115
        usable = bright.any(axis=1)
        if not usable.any():
            bounds[side] = 0
            continue
        masked = np.where(bright[:, :, None], swatches, np.nan)
        colors = np.zeros((len(profile), 3), dtype=np.float64)
        colors[usable] = np.nanmedian(masked[usable], axis=1)
        for channel in range(3):
            colors[:, channel] = np.interp(along, along[usable], colors[usable, channel])
        colors = colors.astype(np.uint8)
        # Median along the boundary stops a tiny adjacent ink mark tinting
        # the cleaned scanner background; blue paper retains its blue tone.
        colors = cv2.medianBlur(colors.reshape(-1, 1, 3), 5).reshape(-1, 3)
        for offset in range(int(profile.max())):
            use = profile > offset
            changed += int(use.sum())
            view[use, offset] = colors[use]
    left, right, top, bottom = bounds
    if not changed or w - left - right < w * .7 or h - top - bottom < h * .7:
        return image, None
    result = Image.fromarray(pixels[top:h - bottom, left:w - right])
    return result, {'edge_crop': [left, top, w - right, h - bottom],
                    'background_pixels': changed, 'edges': [p is not None for p in profiles]}


def process_page(data):
    """Return (complete JPEG bytes, crop metadata); retain uncertain originals."""
    image = decode_jpeg(data)
    metadata = {'cropped': False, 'original_size': list(image.size), 'size': list(image.size),
                'processing_version': PROCESSING_VERSION}
    corners = _paper_corners(image)
    perspective = False
    # At 300 DPI the driver already limits acquisition to A4. With only a
    # narrow strip visible, edge tracking is more precise than forcing four
    # coarse, downscaled corners and resampling the entire document.
    if corners is not None and cv2.contourArea(corners) < image.width * image.height * .93:
        tl, tr, br, bl = corners
        width = round(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
        height = round(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
        if width >= 20 and height >= 20:
            target = np.float32([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]])
            transform = cv2.getPerspectiveTransform(corners, target)
            pixels = cv2.warpPerspective(np.asarray(image), transform, (width, height),
                                         flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT,
                                         borderValue=(255, 255, 255))
            image = Image.fromarray(pixels)
            perspective = True
            metadata['corners'] = [[round(float(v), 2) for v in p] for p in corners]
    image, edges = _remove_external_bands(image, paper_confirmed=perspective)
    if not perspective and edges is None:
        return data, metadata
    output = io.BytesIO()
    image.save(output, 'JPEG', quality=95, subsampling=0)
    metadata.update(cropped=True, size=list(image.size), method='paper-boundary',
                    perspective_corrected=perspective)
    if edges is not None:
        metadata.update(edges)
    return output.getvalue(), metadata
