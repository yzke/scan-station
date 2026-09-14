import io

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw

from image_enhancement import enhance_jpeg


def jpeg(image, **metadata):
    output = io.BytesIO()
    image.save(output, 'JPEG', quality=98, subsampling=0, **metadata)
    return output.getvalue()


def pixels(data):
    with Image.open(io.BytesIO(data)) as image:
        return np.asarray(image.convert('RGB'))


@pytest.fixture
def document():
    width, height = 760, 1024
    y, x = np.mgrid[:height, :width]
    paper = 198 + 35 * x / width + 8 * np.sin(y / height * np.pi)
    noise = np.random.default_rng(7).normal(0, .8, (height, width, 1))
    rgb = np.clip(paper[:, :, None] + [0, 2, 5] + noise, 0, 255).astype('uint8')
    masks = {}

    for name, box, color in [
        ('dark', (55, 80, 650, 84), (28, 28, 28)),
        ('gray', (55, 140, 650, 144), (115, 115, 115)),
        ('faint', (55, 200, 650, 204), None),
        ('solid', (50, 400, 220, 540), (55, 55, 55)),
        ('color_solid', (290, 400, 460, 540), (180, 45, 55)),
    ]:
        mask = Image.new('L', (width, height))
        ImageDraw.Draw(mask).rectangle(box, fill=255)
        masks[name] = np.asarray(mask) > 0
        rgb[masks[name]] = color if color else rgb[masks[name]] - 22

    for name, color, box in [
        ('red', (190, 40, 55), (75, 660, 270, 855)),
        ('blue', (40, 65, 175), (370, 660, 650, 845)),
    ]:
        mask = Image.new('L', (width, height))
        draw = ImageDraw.Draw(mask)
        draw.ellipse(box, outline=255, width=7)
        draw.line((box[0] + 20, box[1] + 80, box[2] - 20, box[3] - 50), fill=255, width=4)
        masks[name] = np.asarray(mask) > 0
        rgb[masks[name]] = color

    all_ink = np.any(list(masks.values()), axis=0).astype('uint8')
    masks['paper'] = cv2.dilate(all_ink, np.ones((19, 19), 'uint8')) == 0
    return jpeg(Image.fromarray(rgb)), masks


def test_original_is_byte_identical_with_metadata(document):
    data, _ = document
    assert enhance_jpeg(data, 'original') is data


def test_enhanced_whitens_uneven_paper_and_keeps_faint_gray_ink(document):
    data, masks = document
    result = pixels(enhance_jpeg(data, 'enhanced'))
    gray = result.mean(axis=2)
    assert gray[masks['paper']].mean() >= 250
    assert np.percentile(gray[masks['paper']], 10) >= 245
    assert gray[masks['dark']].mean() < 75
    assert 70 < gray[masks['gray']].mean() < 170
    assert 180 < gray[masks['faint']].mean() < 240
    assert gray[masks['solid']].mean() < 120


def test_color_enhancement_keeps_red_and_blue_ink_and_solid_logos(document):
    data, masks = document
    result = pixels(enhance_jpeg(data, 'enhanced')).astype('float32')
    red = result[masks['red']].mean(axis=0)
    blue = result[masks['blue']].mean(axis=0)
    solid_red = result[masks['color_solid']].mean(axis=0)
    assert red[0] > red[1] + 100
    assert red[0] > red[2] + 90
    assert blue[2] > blue[0] + 90
    assert blue[2] > blue[1] + 70
    assert solid_red[0] > solid_red[1] + 90


@pytest.mark.parametrize('color, dominant', [((225, 198, 201), 0), ((196, 207, 225), 2)])
def test_enhanced_keeps_pale_colored_stamp_strokes(color, dominant):
    image = Image.new('RGB', (500, 700), (225, 228, 231))
    mask_image = Image.new('L', image.size)
    ImageDraw.Draw(mask_image).ellipse((100, 150, 350, 400), outline=255, width=3)
    mask = np.asarray(mask_image) > 0
    source = np.array(image)
    source[mask] = color
    result = pixels(enhance_jpeg(jpeg(Image.fromarray(source)), 'enhanced')).astype('float32')
    ink = result[mask].mean(axis=0)
    other = 2 if dominant == 0 else 0
    assert ink[dominant] > ink[other] + 15
    assert ink.min() < 238
    assert result[30:80].mean() > 250


def test_bw_retains_faint_lines_color_marks_and_solid_dark_areas(document):
    data, masks = document
    result = pixels(enhance_jpeg(data, 'bw'))
    assert np.array_equal(result[:, :, 0], result[:, :, 1])
    gray = result[:, :, 0]
    assert ((gray < 12) | (gray > 243)).mean() > .995
    assert (gray[masks['paper']] > 243).mean() > .995
    for name in ['dark', 'gray', 'faint', 'red', 'blue', 'solid', 'color_solid']:
        assert (gray[masks[name]] < 128).mean() > .94, name


@pytest.mark.parametrize('mode', ['enhanced', 'bw'])
def test_output_preserves_pixel_geometry_orientation_and_dpi(mode):
    image = Image.new('RGB', (260, 180), '#dedee0')
    ImageDraw.Draw(image).rectangle((12, 17, 60, 21), fill='black')
    exif = Image.Exif()
    exif[274] = 6
    exif[315] = 'Scan station test'
    data = jpeg(image, dpi=(300, 300), exif=exif)
    with Image.open(io.BytesIO(enhance_jpeg(data, mode))) as result:
        assert result.size == image.size
        assert result.info['dpi'] == (300, 300)
        assert result.getexif()[274] == 6
        assert result.getexif()[315] == 'Scan station test'
        assert np.asarray(result.convert('L'))[18:21, 15:58].mean() < 80


@pytest.mark.parametrize('mode', ['enhanced', 'bw'])
@pytest.mark.parametrize('size', [(1, 1), (2, 17), (18, 3)])
def test_tiny_and_grayscale_inputs_are_supported(mode, size):
    result = enhance_jpeg(jpeg(Image.new('L', size, 220)), mode)
    with Image.open(io.BytesIO(result)) as image:
        assert image.size == size
        assert np.asarray(image).mean() > 245


@pytest.mark.parametrize('mode', ['enhanced', 'bw'])
def test_clean_blank_page_does_not_gain_speckles(mode):
    data = jpeg(Image.new('RGB', (500, 700), '#dedede'))
    assert pixels(enhance_jpeg(data, mode)).min() >= 250


def test_bw_suppresses_scanner_grain_without_deleting_decimal_points():
    height, width = 720, 520
    y, x = np.mgrid[:height, :width]
    paper = 206 + 12 * x / width + 4 * np.sin(y / 160)
    noise = np.random.default_rng(12).normal(0, 3.5, (height, width, 3))
    rgb = np.clip(paper[:, :, None] + [0, 2, 6] + noise, 0, 255).astype('uint8')
    rgb[180:182, 70:430] = 100  # A fine table rule.
    rgb[300:304, 170:174] = 40  # Isolated decimal punctuation.
    rgb[300:303, 230:233] = 50
    result = pixels(enhance_jpeg(jpeg(Image.fromarray(rgb)), 'bw'))[:, :, 0]
    assert (result[:160] < 128).mean() < .0005
    assert (result[400:] < 128).mean() < .0005
    assert (result[180:182, 80:420] < 128).mean() > .99
    assert (result[300:304, 170:174] < 128).mean() > .9
    assert (result[300:303, 230:233] < 128).mean() > .9


@pytest.mark.parametrize('mode', ['enhanced', 'bw'])
def test_output_is_deterministic(mode, document):
    data, _ = document
    assert enhance_jpeg(data, mode) == enhance_jpeg(data, mode)


@pytest.mark.parametrize('mode', ['', 'color', 'ENHANCED', None])
def test_unknown_modes_are_rejected(mode):
    with pytest.raises(ValueError):
        enhance_jpeg(b'not an image', mode)


@pytest.mark.parametrize('mode', ['enhanced', 'bw'])
def test_incomplete_or_non_jpeg_inputs_cannot_be_published(mode):
    data = jpeg(Image.new('RGB', (100, 100), 'white'))
    for invalid in [b'', b'not an image', data[:-20]]:
        with pytest.raises(ValueError):
            enhance_jpeg(invalid, mode)
