import io

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from blank_pages import analyze_page


def jpeg(image):
    output = io.BytesIO()
    image.save(output, 'JPEG', quality=95, subsampling=0)
    return output.getvalue()


@pytest.mark.parametrize('color', ['white', '#eeeeeb', '#cadadd', '#99c4d5'])
def test_uniform_light_paper_is_suggested(color):
    assert analyze_page(jpeg(Image.new('RGB', (840, 1188), color)))['is_blank']


def test_paper_with_gradual_shadow_and_sensor_noise_is_suggested():
    rng = np.random.default_rng(73)
    background = 215 + np.linspace(-12, 12, 1188)[:, None] + np.linspace(-4, 4, 840)
    noise = rng.normal(0, 2, (1188, 840))
    pixels = np.clip(background + noise, 0, 255).astype(np.uint8)
    assert analyze_page(jpeg(Image.fromarray(pixels).convert('RGB')))['is_blank']


def test_very_weak_showthrough_is_suggested_for_manual_review():
    image = Image.new('RGB', (840, 1188), '#e1e1e1')
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 140, 730, 1020), outline='#dcdcdc', width=2)
    for y in range(200, 800, 38):
        draw.line((170, y, 640, y), fill='#dcdcdc', width=2)
    assert analyze_page(jpeg(image))['is_blank']


@pytest.mark.parametrize('color', ['#111111', '#cdcdcd'])
def test_single_small_line_of_text_is_not_suggested(color):
    image = Image.new('RGB', (840, 1188), '#e5e5e5')
    draw = ImageDraw.Draw(image)
    draw.text((200, 550), 'Inspection record 2026', fill=color,
              font=ImageFont.truetype('DejaVuSans.ttf', 13))
    assert not analyze_page(jpeg(image))['is_blank']


def test_small_margin_mark_is_not_suggested():
    image = Image.new('RGB', (840, 1188), 'white')
    ImageDraw.Draw(image).line((2, 450, 13, 458, 25, 439), fill='#777777', width=2)
    assert not analyze_page(jpeg(image))['is_blank']


def test_low_contrast_red_stamp_is_not_suggested():
    image = Image.new('RGB', (840, 1188), '#e4e4e4')
    # Similar luminance, but a visible red tint.
    ImageDraw.Draw(image).ellipse((650, 930, 775, 1055), outline='#f0dcdc', width=2)
    assert not analyze_page(jpeg(image))['is_blank']


@pytest.mark.parametrize('size,font_size,text,paper', [
    ((2480, 3508), 25, 'Inspection record 2026', 240),
    ((1240, 1754), 17, '1', 240),
    ((840, 1188), 13, 'Inspection record 2026', 229),
])
def test_faint_small_print_survives_analysis_at_scan_resolutions(size, font_size, text, paper):
    image = Image.new('RGB', size, (paper,) * 3)
    ImageDraw.Draw(image).text((size[0] // 4, size[1] // 2), text,
                              font=ImageFont.truetype('DejaVuSans.ttf', font_size),
                              fill=(paper - 10,) * 3)
    assert not analyze_page(jpeg(image))['is_blank']


@pytest.mark.parametrize('font_size,contrast,text', [(25, 10, '1'), (33, 8, '1'), (25, 8, 'Inspection record 2026')])
def test_faint_sparse_print_is_kept_on_an_otherwise_empty_300_dpi_page(font_size, contrast, text):
    image = Image.new('RGB', (2480, 3508), (240,) * 3)
    ImageDraw.Draw(image).text((620, 1754), text,
                              font=ImageFont.truetype('DejaVuSans.ttf', font_size),
                              fill=(240 - contrast,) * 3)
    assert not analyze_page(jpeg(image))['is_blank']


@pytest.mark.parametrize('color,width', [((250, 232, 232), 2), ((249, 235, 235), 3)])
def test_fine_faint_stamp_survives_downsampling_a_300_dpi_page(color, width):
    image = Image.new('RGB', (2480, 3508), (240,) * 3)
    ImageDraw.Draw(image).ellipse((1900, 2700, 2300, 3100), outline=color, width=width)
    assert not analyze_page(jpeg(image))['is_blank']


def test_blue_paper_with_dark_blue_signature_is_not_suggested():
    image = Image.new('RGB', (840, 1188), '#a9cdd5')
    ImageDraw.Draw(image).line((590, 930, 610, 970, 650, 925, 670, 945), fill='#607eb0', width=2)
    assert not analyze_page(jpeg(image))['is_blank']


def test_filled_rectangle_and_printed_frame_are_not_suggested():
    for frame in [True, False]:
        image = Image.new('RGB', (840, 1188), 'white')
        draw = ImageDraw.Draw(image)
        if frame:
            draw.rectangle((0, 0, 839, 1187), outline='black', width=3)
        else:
            draw.rectangle((230, 300, 620, 820), fill='black')
        assert not analyze_page(jpeg(image))['is_blank']


@pytest.mark.parametrize('level', [200, 210, 220, 230])
def test_broad_light_gray_regions_are_page_content(level):
    image = Image.new('RGB', (1240, 1754), (240,) * 3)
    ImageDraw.Draw(image).rectangle((350, 550, 850, 1150), fill=(level,) * 3)
    assert not analyze_page(jpeg(image))['is_blank']


def test_thin_residual_scanner_strip_is_not_treated_as_page_content():
    image = Image.new('RGB', (840, 1188), '#eeeeee')
    ImageDraw.Draw(image).rectangle((0, 0, 2, 1187), fill='#303030')
    assert analyze_page(jpeg(image))['is_blank']


def test_printed_line_near_edge_is_kept_when_contrast_mask_reaches_margin():
    image = Image.new('RGB', (840, 1188), '#eeeeee')
    ImageDraw.Draw(image).line((5, 30, 5, 1160), fill='#777777', width=2)
    assert not analyze_page(jpeg(image))['is_blank']


@pytest.mark.parametrize('color', ['black', '#555555'])
def test_dark_or_unknown_pages_are_kept(color):
    assert not analyze_page(jpeg(Image.new('RGB', (840, 1188), color)))['is_blank']


def test_incomplete_jpeg_is_an_error_and_original_bytes_are_unchanged():
    data = jpeg(Image.new('RGB', (840, 1188), 'white'))
    original = bytes(data)
    assert analyze_page(data)['is_blank']
    assert data == original
    with pytest.raises((ValueError, OSError)):
        analyze_page(data[:-20])
