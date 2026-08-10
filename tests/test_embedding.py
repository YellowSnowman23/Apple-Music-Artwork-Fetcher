import base64
import io
import subprocess
from pathlib import Path

import pytest
from mutagen.aiff import AIFF
from mutagen.apev2 import APEBinaryValue
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, TIT2, PictureType
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.wave import WAVE
from mutagen.wavpack import WavPack
from PIL import Image

from apple_artwork import EmbedError, decode_artwork, embed_artwork


def make_audio(path: Path, codec: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            "0.25",
            "-c:a",
            codec,
            str(path),
        ],
        check=True,
    )


def image_bytes(color: tuple[int, int, int], image_format: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (48, 48), color).save(buffer, format=image_format)
    return buffer.getvalue()


def flac_picture(data: bytes, picture_type: PictureType) -> Picture:
    picture = Picture()
    picture.type = picture_type
    picture.mime = "image/png"
    picture.width = 48
    picture.height = 48
    picture.depth = 24
    picture.data = data
    return picture


def test_embed_flac_replaces_only_front_cover_and_preserves_other_metadata(tmp_path: Path) -> None:
    path = tmp_path / "track.flac"
    make_audio(path, "flac")
    old_front = image_bytes((255, 0, 0))
    old_back = image_bytes((0, 0, 255))
    new_front = image_bytes((0, 255, 0))
    audio = FLAC(path)
    audio["title"] = "Do Not Lose Me"
    audio.add_picture(flac_picture(old_front, PictureType.COVER_FRONT))
    audio.add_picture(flac_picture(old_back, PictureType.COVER_BACK))
    audio.save()
    artwork = decode_artwork(new_front, "https://a5.mzstatic.com/master.png")

    result = embed_artwork(path, artwork, replace_existing=True)

    updated = FLAC(path)
    assert result.status == "embedded"
    assert updated["title"] == ["Do Not Lose Me"]
    assert [
        picture.data for picture in updated.pictures if picture.type == PictureType.COVER_FRONT
    ] == [new_front]
    assert [
        picture.data for picture in updated.pictures if picture.type == PictureType.COVER_BACK
    ] == [old_back]


@pytest.mark.parametrize(
    ("suffix", "codec", "loader"),
    (
        (".mp3", "libmp3lame", MP3),
        (".wav", "pcm_s16le", WAVE),
        (".aiff", "pcm_s16be", AIFF),
    ),
)
def test_embed_first_artwork_into_tagless_id3_container(
    tmp_path: Path,
    suffix: str,
    codec: str,
    loader: type[MP3] | type[WAVE] | type[AIFF],
) -> None:
    path = tmp_path / f"tagless{suffix}"
    make_audio(path, codec)
    initial = loader(path)
    initial.delete()
    assert loader(path).tags is None
    new_front = image_bytes((0, 255, 0))
    artwork = decode_artwork(new_front, "https://a5.mzstatic.com/master.png")

    result = embed_artwork(path, artwork)

    updated = loader(path)
    assert result.status == "embedded"
    assert updated.tags is not None
    assert [
        picture.data
        for picture in updated.tags.getall("APIC")
        if picture.type == PictureType.COVER_FRONT
    ] == [new_front]


def test_embed_mp3_replaces_front_apic_and_preserves_other_id3_frames(tmp_path: Path) -> None:
    path = tmp_path / "track.mp3"
    make_audio(path, "libmp3lame")
    old_front = image_bytes((255, 0, 0), "JPEG")
    old_back = image_bytes((0, 0, 255), "JPEG")
    new_front = image_bytes((0, 255, 0), "JPEG")
    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags.add(TIT2(encoding=3, text=["Keep This Title"]))
    audio.tags.add(
        APIC(
            encoding=3,
            mime="image/jpeg",
            type=PictureType.COVER_FRONT,
            desc="front",
            data=old_front,
        )
    )
    audio.tags.add(
        APIC(
            encoding=3,
            mime="image/jpeg",
            type=PictureType.COVER_BACK,
            desc="back",
            data=old_back,
        )
    )
    audio.save(v2_version=3)
    artwork = decode_artwork(new_front, "https://a5.mzstatic.com/master.jpg")

    result = embed_artwork(path, artwork, replace_existing=True)

    updated = MP3(path)
    assert updated.tags is not None
    pictures = updated.tags.getall("APIC")
    assert result.status == "embedded"
    assert str(updated.tags["TIT2"]) == "Keep This Title"
    assert [picture.data for picture in pictures if picture.type == PictureType.COVER_FRONT] == [
        new_front
    ]
    assert [picture.data for picture in pictures if picture.type == PictureType.COVER_BACK] == [
        old_back
    ]


def test_embed_m4a_replaces_covr_and_preserves_mp4_metadata(tmp_path: Path) -> None:
    path = tmp_path / "track.m4a"
    make_audio(path, "alac")
    old_front = image_bytes((255, 0, 0), "PNG")
    new_front = image_bytes((0, 255, 0), "PNG")
    audio = MP4(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags["\xa9nam"] = ["Keep This Title"]
    audio.tags["covr"] = [MP4Cover(old_front, imageformat=MP4Cover.FORMAT_PNG)]
    audio.save()
    artwork = decode_artwork(new_front, "https://a5.mzstatic.com/master.png")

    result = embed_artwork(path, artwork, replace_existing=True)

    updated = MP4(path)
    assert updated.tags is not None
    assert result.status == "embedded"
    assert updated.tags["\xa9nam"] == ["Keep This Title"]
    assert [bytes(cover) for cover in updated.tags["covr"]] == [new_front]


def test_embed_opus_replaces_front_picture_and_preserves_vorbis_comments(tmp_path: Path) -> None:
    path = tmp_path / "track.opus"
    make_audio(path, "libopus")
    old_front = image_bytes((255, 0, 0), "PNG")
    old_back = image_bytes((0, 0, 255), "PNG")
    new_front = image_bytes((0, 255, 0), "PNG")
    front_picture = flac_picture(old_front, PictureType.COVER_FRONT)
    back_picture = flac_picture(old_back, PictureType.COVER_BACK)
    audio = OggOpus(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags["title"] = ["Keep This Title"]
    audio.tags["metadata_block_picture"] = [
        base64.b64encode(front_picture.write()).decode("ascii"),
        base64.b64encode(back_picture.write()).decode("ascii"),
    ]
    audio.save()
    artwork = decode_artwork(new_front, "https://a5.mzstatic.com/master.png")

    result = embed_artwork(path, artwork, replace_existing=True)

    updated = OggOpus(path)
    assert updated.tags is not None
    pictures = [
        Picture(base64.b64decode(value)) for value in updated.tags["metadata_block_picture"]
    ]
    assert result.status == "embedded"
    assert updated.tags["title"] == ["Keep This Title"]
    assert [picture.data for picture in pictures if picture.type == PictureType.COVER_FRONT] == [
        new_front
    ]
    assert [picture.data for picture in pictures if picture.type == PictureType.COVER_BACK] == [
        old_back
    ]


def test_embed_wave_writes_and_verifies_id3_front_cover(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    make_audio(path, "pcm_s16le")
    new_front = image_bytes((0, 255, 0), "PNG")
    audio = WAVE(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags.add(TIT2(encoding=3, text=["Keep This Title"]))
    audio.save()
    artwork = decode_artwork(new_front, "https://a5.mzstatic.com/master.png")

    result = embed_artwork(path, artwork)

    updated = WAVE(path)
    assert updated.tags is not None
    pictures = updated.tags.getall("APIC")
    assert result.status == "embedded"
    assert str(updated.tags["TIT2"]) == "Keep This Title"
    assert [picture.data for picture in pictures if picture.type == PictureType.COVER_FRONT] == [
        new_front
    ]


def test_embed_opus_refuses_malformed_picture_metadata_instead_of_deleting_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "malformed.opus"
    make_audio(path, "libopus")
    audio = OggOpus(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags["metadata_block_picture"] = ["not-valid-base64!!!"]
    audio.save()
    artwork = decode_artwork(image_bytes((0, 255, 0), "PNG"), "https://a5.mzstatic.com/master.png")

    with pytest.raises(EmbedError, match="malformed METADATA_BLOCK_PICTURE"):
        embed_artwork(path, artwork, replace_existing=True)

    unchanged = OggOpus(path)
    assert unchanged.tags is not None
    assert unchanged.tags["metadata_block_picture"] == ["not-valid-base64!!!"]


def test_embed_mp3_preserves_existing_id3v1_bytes_exactly(tmp_path: Path) -> None:
    path = tmp_path / "id3v1.mp3"
    make_audio(path, "libmp3lame")
    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags.add(TIT2(encoding=1, text=["Modern V2 Title"]))
    audio.save(v2_version=3)
    id3v1 = b"".join(
        [
            b"TAG",
            b"Original V1 Title".ljust(30, b"\0"),
            b"Original Artist".ljust(30, b"\0"),
            b"Original Album".ljust(30, b"\0"),
            b"1999",
            b"Original Comment".ljust(30, b"\0"),
            bytes([13]),
        ]
    )
    assert len(id3v1) == 128
    with path.open("ab") as handle:
        handle.write(id3v1)
    artwork = decode_artwork(image_bytes((0, 255, 0), "JPEG"), "https://a5.mzstatic.com/master.jpg")

    embed_artwork(path, artwork)

    with path.open("rb") as handle:
        handle.seek(-128, 2)
        assert handle.read(128) == id3v1


def test_embed_aiff_writes_and_verifies_id3_front_cover(tmp_path: Path) -> None:
    path = tmp_path / "track.aiff"
    make_audio(path, "pcm_s16be")
    new_front = image_bytes((0, 255, 0), "PNG")
    audio = AIFF(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags.add(TIT2(encoding=3, text=["Keep This Title"]))
    audio.save()
    artwork = decode_artwork(new_front, "https://a5.mzstatic.com/master.png")

    result = embed_artwork(path, artwork)

    updated = AIFF(path)
    assert updated.tags is not None
    pictures = updated.tags.getall("APIC")
    assert result.status == "embedded"
    assert result.format == "AIFF"
    assert str(updated.tags["TIT2"]) == "Keep This Title"
    assert [picture.data for picture in pictures if picture.type == PictureType.COVER_FRONT] == [
        new_front
    ]


def test_embed_wavpack_replaces_front_and_preserves_back_artwork(tmp_path: Path) -> None:
    path = tmp_path / "track.wv"
    make_audio(path, "wavpack")
    old_front = image_bytes((255, 0, 0), "JPEG")
    old_back = image_bytes((0, 0, 255), "PNG")
    new_front = image_bytes((0, 255, 0), "PNG")
    audio = WavPack(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags["Title"] = "Keep This Title"
    audio.tags["Cover Art (Front)"] = APEBinaryValue(b"front.jpg\0" + old_front)
    audio.tags["Cover Art (Back)"] = APEBinaryValue(b"back.png\0" + old_back)
    audio.save()
    artwork = decode_artwork(new_front, "https://a5.mzstatic.com/master.png")

    result = embed_artwork(path, artwork, replace_existing=True)

    updated = WavPack(path)
    assert updated.tags is not None
    assert result.status == "embedded"
    assert result.format == "WavPack"
    assert str(updated.tags["Title"]) == "Keep This Title"
    assert bytes(updated.tags["Cover Art (Back)"]) == b"back.png\0" + old_back
    assert bytes(updated.tags["Cover Art (Front)"]) == b"cover.png\0" + new_front


def test_embedding_failure_never_corrupts_the_original_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rollback.mp3"
    make_audio(path, "libmp3lame")
    original = path.read_bytes()
    artwork = decode_artwork(image_bytes((0, 255, 0), "JPEG"), "https://a5.mzstatic.com/master.jpg")

    def corrupt_then_fail(audio: MP3, *args: object, **kwargs: object) -> None:
        del audio, kwargs
        staged = args[0]
        assert hasattr(staged, "seek") and hasattr(staged, "truncate")
        staged.seek(0)  # type: ignore[union-attr]
        staged.truncate(0)  # type: ignore[union-attr]
        staged.write(b"corrupted staged file")  # type: ignore[union-attr]
        staged.flush()  # type: ignore[union-attr]
        raise OSError("simulated disk failure")

    monkeypatch.setattr(MP3, "save", corrupt_then_fail)

    with pytest.raises(EmbedError, match="simulated disk failure"):
        embed_artwork(path, artwork)

    assert path.read_bytes() == original
