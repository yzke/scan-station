import io

import numpy as np
import pytest
from PIL import Image, ImageDraw

from image_processing import process_page


def jpeg(image):
    out = io.BytesIO()
    image.save(out, 'JPEG', quality=97)
    return out.getvalue()


def test_paper_with_white_feeder_stripe_preserves_stamps_and_edge_marks():
    image = Image.new('RGB', (650, 1125), '#242429')
    draw = ImageDraw.Draw(image)
    draw.line((325, 0, 325, 1000), '#eeeeee', width=2)
    draw.rectangle((0, 980, 650, 1125), fill='black')
    draw.polygon([(13, 42), (633, 46), (628, 921), (10, 916)], fill='#ededeb')
    draw.rectangle((35, 90, 600, 850), outline='#444444', width=2)
    draw.ellipse((430, 740, 596, 896), outline='#c03232', width=5)
    draw.rectangle((18, 400, 25, 420), fill='#c03232')
    data, metadata = process_page(jpeg(image))
    result = np.asarray(Image.open(io.BytesIO(data)))
    assert metadata['cropped']
    assert 600 < result.shape[1] < 630
    assert 855 < result.shape[0] < 895
    # A stamp near the bottom/right and a tiny margin mark both survive.
    red = (result[:, :, 0] > result[:, :, 1] * 1.3) & (result[:, :, 0] > 90)
    assert red[-220:, -220:].sum() > 1500
    assert red[330:450, :30].sum() > 60
    assert (result.mean(axis=2) < 60).mean() < .025


@pytest.mark.parametrize('color', ['white', 'black', '#555555', '#b1c1d1'])
def test_uniform_pages_are_unchanged(color):
    data = jpeg(Image.new('RGB', (500, 700), color))
    processed, metadata = process_page(data)
    assert processed == data
    assert not metadata['cropped']


def test_page_content_and_printed_black_frame_are_unchanged():
    image = Image.new('RGB', (500, 700), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 490, 690), outline='black', width=4)
    draw.rectangle((40, 120, 200, 550), fill='black')
    data = jpeg(image)
    assert process_page(data)[0] == data


def test_partial_jpeg_cannot_be_published():
    data = jpeg(Image.new('RGB', (500, 700), 'white'))
    with pytest.raises((ValueError, OSError)):
        process_page(data[:-20])


def test_uncertain_small_bright_region_does_not_destroy_a_dark_page():
    image = Image.new('RGB', (500, 700), '#151515')
    ImageDraw.Draw(image).rectangle((70, 100, 170, 220), fill='white')
    data = jpeg(image)
    assert process_page(data)[0] == data


@pytest.mark.parametrize('turns', range(4))
def test_narrow_curved_band_is_removed_without_moving_or_resampling_content(turns):
    from image_processing import _remove_external_bands
    pixels = np.full((900, 600, 3), (210, 220, 228), dtype=np.uint8)
    for y in range(900):
        edge = 15 + round(4 * np.sin(y / 900 * np.pi))
        pixels[y, :edge] = 35
        pixels[y, edge] = 140  # Mixed scanner-background / paper pixel.
    # Black text and a red mark only a few pixels inside the actual sheet.
    pixels[220:240, 23:45] = 25
    pixels[620:642, 22:40] = (185, 30, 40)
    original = np.rot90(pixels, turns).copy()
    result, metadata = _remove_external_bands(Image.fromarray(original))
    assert metadata is not None
    left, top, right, bottom = metadata['edge_crop']
    actual = np.asarray(result)
    expected = original[top:bottom, left:right]
    # Actual foreground colors and geometry survive exactly before encoding.
    foreground = (expected[:, :, 0] == 25) | (expected[:, :, 0] == 185)
    assert foreground.sum() == 20 * 22 + 22 * 18
    assert np.array_equal(actual[foreground], expected[foreground])
    assert (actual.mean(axis=2) < 100).sum() == 20 * 22 + 22 * 18


@pytest.mark.parametrize('stripe_width', [2, 5, 8])
def test_feeder_alignment_stripe_is_not_mistaken_for_full_height_paper(stripe_width):
    image = Image.new('RGB', (600, 900), '#242429')
    draw = ImageDraw.Draw(image)
    draw.rectangle((5, 0, 5 + stripe_width - 1, 899), fill='white')
    draw.rectangle((30, 0, 599, 899), fill='#dddfe1')
    result, metadata = process_page(jpeg(image))
    pixels = np.asarray(Image.open(io.BytesIO(result)))
    assert metadata['cropped']
    assert 565 <= pixels.shape[1] <= 570
    assert pixels.mean(axis=2).min() > 160


def test_frame_touching_image_edges_is_unchanged_without_background_evidence():
    image = Image.new('RGB', (600, 900), 'white')
    ImageDraw.Draw(image).rectangle((0, 0, 599, 899), outline='black', width=4)
    data = jpeg(image)
    assert process_page(data)[0] == data


def test_300dpi_single_sided_black_strip_keeps_resolution_and_blue_paper():
    image = Image.new('RGB', (2480, 3508), '#93c4d1')
    ImageDraw.Draw(image).rectangle((0, 0, 31, 3507), fill='#242429')
    result, metadata = process_page(jpeg(image))
    output = np.asarray(Image.open(io.BytesIO(result)))
    assert metadata['cropped'] and not metadata['perspective_corrected']
    assert metadata['size'] == [2448, 3508]
    assert output[:, :5, :].mean(axis=(0, 1)) == pytest.approx((147, 196, 209), abs=3)
