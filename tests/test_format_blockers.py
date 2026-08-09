import io
import os
import subprocess
from pathlib import Path

import pytest
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TIT2, PictureType
from mutagen.mp3 import MP3
from mutagen.wave import WAVE
from PIL import Image

import apple_artwork
from apple_artwork import EmbedError, EmbedResult, decode_artwork, embed_artwork


def make_audio(path: Path, codec: str) -> None:
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
            "0.2",
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


def artwork(image_format: str = "PNG"):
    extension = "png" if image_format == "PNG" else "jpg"
    return decode_artwork(
        image_bytes((0, 255, 0), image_format),
        f"https://a5.mzstatic.com/cover.{extension}",
    )


def test_mp3_preserves_nonfront_apic_when_description_matches_new_default(tmp_path: Path) -> None:
    path = tmp_path / "collision.mp3"
    make_audio(path, "libmp3lame")
    old_front = image_bytes((255, 0, 0), "JPEG")
    back = image_bytes((0, 0, 255), "JPEG")
    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags.add(
        APIC(
            encoding=3,
            mime="image/jpeg",
            type=PictureType.COVER_FRONT,
            desc="old front",
            data=old_front,
        )
    )
    audio.tags.add(
        APIC(
            encoding=3,
            mime="image/jpeg",
            type=PictureType.COVER_BACK,
            desc="Front cover",
            data=back,
        )
    )
    audio.save(v2_version=4)

    embed_artwork(path, artwork("JPEG"), replace_existing=True)

    updated = MP3(path)
    assert updated.tags is not None
    pictures = updated.tags.getall("APIC")
    assert [frame.data for frame in pictures if frame.type == PictureType.COVER_BACK] == [back]
    assert len([frame for frame in pictures if frame.type == PictureType.COVER_FRONT]) == 1


def test_wavpack_with_id3v1_tail_is_refused_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "tail.wv"
    make_audio(path, "wavpack")
    id3v1 = b"TAG" + b"preserve".ljust(125, b"\0")
    with path.open("ab") as handle:
        handle.write(id3v1)
    before = path.read_bytes()

    with pytest.raises(EmbedError, match=r"ID3v1|tail"):
        embed_artwork(path, artwork(), replace_existing=True)

    assert path.read_bytes() == before


def test_video_bearing_mp4_container_is_refused_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=16x16:r=1",
            "-t",
            "0.2",
            "-c:a",
            "aac",
            "-c:v",
            "mpeg4",
            str(path),
        ],
        check=True,
    )
    before = path.read_bytes()

    with pytest.raises(EmbedError, match=r"audio track|video|MP4"):
        embed_artwork(path, artwork(), replace_existing=True)

    assert path.read_bytes() == before


def test_flac_with_competing_picture_comment_store_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "competing.flac"
    make_audio(path, "flac")
    picture = Picture()
    picture.type = PictureType.COVER_FRONT
    picture.mime = "image/png"
    picture.width = picture.height = 48
    picture.depth = 24
    picture.data = image_bytes((255, 0, 0))
    audio = FLAC(path)
    audio["metadata_block_picture"] = [
        __import__("base64").b64encode(picture.write()).decode("ascii")
    ]
    audio.save()
    before = path.read_bytes()

    with pytest.raises(EmbedError, match=r"competing|METADATA_BLOCK_PICTURE"):
        embed_artwork(path, artwork(), replace_existing=True)

    assert path.read_bytes() == before


def test_concurrent_source_edit_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "concurrent.mp3"
    make_audio(path, "libmp3lame")
    concurrent = b"concurrent replacement"

    def staged_success(
        _temporary: int,
        _artwork: object,
        *,
        replace_existing: bool = False,
        display_path: Path | None = None,
    ) -> EmbedResult:
        del replace_existing, display_path
        path.write_bytes(concurrent)
        return EmbedResult("embedded", "MP3", "simulated staged success")

    monkeypatch.setattr(apple_artwork, "_embed_artwork_in_place", staged_success)

    with pytest.raises(EmbedError, match=r"changed|concurrent"):
        embed_artwork(path, artwork("JPEG"), replace_existing=True)

    assert path.read_bytes() == concurrent


def test_postwrite_metadata_change_aborts_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "metadata.mp3"
    make_audio(path, "libmp3lame")
    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags.add(TIT2(encoding=3, text=["Original title"]))
    audio.save()
    before = path.read_bytes()
    real_embed = apple_artwork._embed_artwork_in_place

    def corrupt_metadata(
        temporary: int,
        new_artwork: object,
        *,
        replace_existing: bool = False,
        display_path: Path | None = None,
    ) -> EmbedResult:
        result = real_embed(
            temporary,
            new_artwork,
            replace_existing=replace_existing,
            display_path=display_path,
        )
        with os.fdopen(os.dup(temporary), "r+b") as handle:
            handle.seek(0)
            staged = MP3(handle)
            assert staged.tags is not None
            staged.tags.setall("TIT2", [TIT2(encoding=3, text=["Corrupted title"])])
            handle.seek(0)
            staged.save(handle)
        return result

    monkeypatch.setattr(apple_artwork, "_embed_artwork_in_place", corrupt_metadata)

    with pytest.raises(EmbedError, match=r"metadata|preserv"):
        embed_artwork(path, artwork("JPEG"), replace_existing=True)

    assert path.read_bytes() == before


def test_id3v22_is_refused_for_every_id3_backed_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "track.wav"
    make_audio(path, "pcm_s16le")
    wave = WAVE(path)
    if wave.tags is None:
        wave.add_tags()
    assert wave.tags is not None
    wave.tags.add(TIT2(encoding=3, text=["Version probe"]))
    wave.save()
    real_file = apple_artwork.mutagen.File

    def fake_file(candidate: Path, *args: object, **kwargs: object):
        audio = real_file(candidate, *args, **kwargs)
        if isinstance(audio, WAVE):
            tags = ID3()
            tags._version = (2, 2, 0)
            audio.tags = tags
        return audio

    monkeypatch.setattr(apple_artwork.mutagen, "File", fake_file)

    with pytest.raises(EmbedError, match=r"ID3v2.2"):
        apple_artwork.preflight_artwork(path, artwork(), replace_existing=True)
