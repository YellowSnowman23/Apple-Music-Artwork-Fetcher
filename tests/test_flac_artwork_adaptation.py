import io

import pytest
from PIL import Image

from apple_music_artwork.adapters.flac import (
    FLAC_PICTURE_BLOCK_MAX_BYTES,
    _flac_picture_payload_size,
    derive_flac_artwork,
)
from apple_music_artwork.artwork import decode_artwork
from apple_music_artwork.models import ArtworkError


def patterned_artwork(
    size: tuple[int, int] = (128, 128),
    *,
    alpha: bool = False,
):
    state = 1
    pixels = bytearray()
    for _y in range(size[1]):
        for x in range(size[0]):
            for _channel in range(3):
                state = (1_664_525 * state + 1_013_904_223) & 0xFFFFFFFF
                pixels.append(state >> 24)
            if alpha:
                pixels.append(0 if x < size[0] // 2 else 255)
    image = Image.frombytes("RGBA" if alpha else "RGB", size, bytes(pixels))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return decode_artwork(output.getvalue(), "https://a5.mzstatic.com/source.png")


def test_flac_derivation_keeps_exact_source_when_picture_fits() -> None:
    source = patterned_artwork((64, 64))

    result = derive_flac_artwork(source)

    assert result.artwork is source
    assert result.transformation == "source"
    assert result.jpeg_quality is None
    assert result.alpha_matte is None
    assert result.dimensions_retained is True
    assert result.source_bytes == len(source.data)
    assert _flac_picture_payload_size(result.artwork) <= FLAC_PICTURE_BLOCK_MAX_BYTES


def test_flac_derivation_reencodes_jpeg_at_same_dimensions_before_downscaling() -> None:
    source = patterned_artwork()

    result = derive_flac_artwork(source, maximum_picture_bytes=18_000)

    assert result.artwork.mime == "image/jpeg"
    assert (result.artwork.width, result.artwork.height) == (source.width, source.height)
    assert result.transformation == "jpeg_reencoded"
    assert result.jpeg_quality == 95
    assert result.dimensions_retained is True
    assert result.source_sha256 == source.sha256
    assert _flac_picture_payload_size(result.artwork) <= 18_000


def test_flac_derivation_uses_bounded_quality_ladder_at_same_dimensions() -> None:
    source = patterned_artwork()

    result = derive_flac_artwork(source, maximum_picture_bytes=16_000)

    assert (result.artwork.width, result.artwork.height) == (source.width, source.height)
    assert result.transformation == "jpeg_reencoded"
    assert result.jpeg_quality == 92
    assert _flac_picture_payload_size(result.artwork) <= 16_000


def test_flac_derivation_downscales_with_lanczos_only_after_quality_ladder_fails() -> None:
    source = patterned_artwork()

    result = derive_flac_artwork(source, maximum_picture_bytes=5_000)

    assert result.artwork.mime == "image/jpeg"
    assert result.artwork.width < source.width
    assert result.artwork.height < source.height
    assert result.transformation == "jpeg_reencoded_downscaled"
    assert result.dimensions_retained is False
    assert result.jpeg_quality in {95, 92, 90, 88, 85, 82, 80}
    assert _flac_picture_payload_size(result.artwork) <= 5_000


def test_flac_derivation_flattens_transparency_on_documented_white_matte() -> None:
    source = patterned_artwork((96, 96), alpha=True)

    first = derive_flac_artwork(source, maximum_picture_bytes=15_000)
    second = derive_flac_artwork(source, maximum_picture_bytes=15_000)

    assert first.artwork.data == second.artwork.data
    assert first.alpha_matte == "#FFFFFF"
    assert (first.artwork.width, first.artwork.height) == (source.width, source.height)
    with Image.open(io.BytesIO(first.artwork.data)) as image:
        red, green, blue = image.convert("RGB").getpixel((10, 48))
    assert min(red, green, blue) >= 240


def test_flac_derivation_rejects_limits_outside_the_flac_spec() -> None:
    source = patterned_artwork((64, 64))

    with pytest.raises(ArtworkError, match="FLAC PICTURE limit"):
        derive_flac_artwork(source, maximum_picture_bytes=FLAC_PICTURE_BLOCK_MAX_BYTES + 1)


def test_flac_derivation_fails_instead_of_shrinking_below_minimum_dimensions() -> None:
    source = patterned_artwork((64, 64))

    with pytest.raises(ArtworkError, match="unable to fit artwork"):
        derive_flac_artwork(source, maximum_picture_bytes=100)
