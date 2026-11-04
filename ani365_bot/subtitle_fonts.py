"""Optional deployment-local font files for the libass worker.

Fonts live outside the source tree in DATA_DIR/subtitle-fonts. The manifest maps
real family names to all their installed faces; fontconfig selects weight/style.
"""
import json
import re
from pathlib import Path


def subtitle_fonts(data_dir):
    directory = Path(data_dir) / "subtitle-fonts"
    try:
        manifest = directory / "fonts.json"
        if manifest.stat().st_size > 128 * 1024:
            return {}
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    result = {}
    for family, filenames in list(data.items())[:256]:
        if not isinstance(family, str) or not isinstance(filenames, list):
            continue
        files = []
        for name in filenames[:16]:
            if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+\.(?:ttf|otf|woff2?)", name):
                continue
            path = directory / name
            try:
                if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 16 * 1024 * 1024:
                    continue
            except OSError:
                continue
            files.append(name)
        if files:
            result[family.strip().lstrip("@").casefold()] = files
    return result
