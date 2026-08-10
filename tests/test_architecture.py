import ast
from importlib import import_module
from pathlib import Path

import apple_artwork
import apple_music_artwork
from apple_music_artwork.adapters import ADAPTERS
from apple_music_artwork.adapters.base import FormatAdapter

MODULES = (
    "artwork",
    "catalog",
    "cli",
    "constants",
    "embedding",
    "filesystem",
    "matching",
    "metadata",
    "models",
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
