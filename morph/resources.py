"""Read-only assets in source checkouts and installed wheels.

Mutable projects and caches belong in the user's data directory, never here.
Normal pip installations unpack wheels; zip-imported installations are unsupported.
"""
from pathlib import Path


def asset_path(*parts: str) -> Path:
    """Return an existing bundled asset, independent of the working directory."""
    relative = Path(*parts)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Asset paths must stay inside the resource directory")
    package = Path(__file__).resolve().parent
    root = package / "_assets"
    if not root.is_dir():
        root = package.parent  # developer checkout
    path = root / relative
    if not path.exists():
        raise FileNotFoundError(f"Missing R Cubed resource: {relative}")
    return path
