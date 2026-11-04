#!/usr/bin/env python3
"""Install existing original font faces for an ASS into a deployment data folder.

Requires fc-scan (fontconfig). Does not download fonts or replace missing families.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess


def required_families(script):
    result = set()
    section = ""
    fields = []
    for line in script.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line.casefold()
        elif section in {"[v4 styles]", "[v4+ styles]"}:
            if line.casefold().startswith("format:"):
                fields = [item.strip().casefold() for item in line.split(":", 1)[1].split(",")]
            elif line.casefold().startswith("style:") and "fontname" in fields:
                values = next(csv.reader([line.split(":", 1)[1]]))
                if len(values) > fields.index("fontname"):
                    result.add(values[fields.index("fontname")].strip().lstrip("@"))
        elif section == "[events]" and line.casefold().startswith("dialogue:"):
            for block in re.findall(r"\{([^}]*)\}", line):
                result.update(name.strip().lstrip("@") for name in re.findall(r"\\fn([^\\}]+)", block) if name.strip())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ass", type=Path)
    parser.add_argument("--source", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, default=Path("data/subtitle-fonts"))
    args = parser.parse_args()
    required = required_families(args.ass.read_text(encoding="utf-8-sig"))
    wanted = {name.casefold(): name for name in required}
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "fonts.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    found = {}
    for directory in args.source:
        for source in sorted(directory.rglob("*")):
            if source.suffix.lower() not in {".ttf", ".otf", ".woff", ".woff2"} or not source.is_file():
                continue
            family = subprocess.run(["fc-scan", "--format", "%{family[0]}", str(source)],
                                    capture_output=True, text=True, check=True).stdout.strip()
            key = family.casefold()
            if key not in wanted:
                continue
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            name = digest + source.suffix.lower()
            shutil.copyfile(source, args.output / name)
            found.setdefault(key, set()).add(name)
    for family, names in found.items():
        manifest[family] = sorted(names)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for key, name in sorted(wanted.items()):
        count = sum((args.output / item).is_file() for item in manifest.get(key, []))
        print(f"{name}: {count} faces installed" if count else f"{name}: MISSING (no replacement installed)")


if __name__ == "__main__":
    main()
