import ast
import re
from importlib import import_module
from pathlib import Path

import pytest

import apple_artwork
import apple_music_artwork
from apple_music_artwork.adapters import ADAPTERS
from apple_music_artwork.adapters.base import FormatAdapter
from apple_music_artwork.artwork import ArtworkDownloader
from apple_music_artwork.catalog import AppleCatalogClient
from apple_music_artwork.cli import main
from apple_music_artwork.constants import REPORT_SCHEMA_VERSION, USER_AGENT, VERSION

MODULES = (
    "artwork",
    "catalog",
    "cli",
    "constants",
    "embedding",
    "filesystem",
    "folder_artwork",
    "matching",
    "metadata",
    "models",
    "musicbrainz",
    "mutagen_io",
    "network",
    "pipeline",
    "reports",
)


def test_domains_and_legacy_exports() -> None:
    for name in MODULES:
        import_module(f"apple_music_artwork.{name}")
    for name in apple_music_artwork.__all__:
        assert getattr(apple_artwork, name) is getattr(apple_music_artwork, name)


def test_registered_adapters_are_separate_implementations() -> None:
    assert tuple(adapter.format_name for adapter in ADAPTERS) == (
        "Xiph",
        "MP4",
        "ID3",
        "WavPack",
        "FLAC",
    )
    assert len({type(adapter).__module__ for adapter in ADAPTERS}) == len(ADAPTERS)
    for adapter in ADAPTERS:
        adapter_type = type(adapter)
        assert adapter_type.front_pictures is not FormatAdapter.front_pictures
        assert adapter_type.embed is not FormatAdapter.embed


def test_package_never_imports_legacy_facade() -> None:
    package_root = Path(apple_music_artwork.__file__).parent
    offenders: list[str] = []
    for source_path in package_root.rglob("*.py"):
        for node in ast.walk(ast.parse(source_path.read_text())):
            if isinstance(node, ast.Import):
                if any(alias.name == "apple_artwork" for alias in node.names):
                    offenders.append(str(source_path))
            elif isinstance(node, ast.ImportFrom) and node.module == "apple_artwork":
                offenders.append(str(source_path))
    assert not offenders


def test_network_clients_identify_current_release(tmp_path: Path) -> None:
    class Session:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

    catalog_session = Session()
    artwork_session = Session()

    AppleCatalogClient(cache_dir=tmp_path, session=catalog_session)
    ArtworkDownloader(cache_dir=tmp_path, session=artwork_session)

    assert catalog_session.headers["User-Agent"] == USER_AGENT
    assert artwork_session.headers["User-Agent"] == USER_AGENT


def test_release_version_is_consistent_with_project_metadata() -> None:
    project_text = (Path(apple_music_artwork.__file__).parent.parent / "pyproject.toml").read_text()
    project_version = re.search(r'^version = "([^"]+)"$', project_text, re.MULTILINE)

    assert project_version is not None
    assert project_version.group(1) == VERSION == "3.1.0"
    assert REPORT_SCHEMA_VERSION == 4


def test_cli_reports_current_release(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match="0"):
        main(["--version"])

    assert capsys.readouterr().out == f"apple-artwork {VERSION}\n"
