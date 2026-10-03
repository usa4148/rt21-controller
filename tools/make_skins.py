#!/usr/bin/env python3
"""Turn the raw compass-rose sources into the portable skins the app ships.

    pip install -r tools/requirements.txt
    python3 tools/make_skins.py            # skins/raw/* + skins/sources.json -> skins/*.webp + skins/skins.json
    python3 tools/make_skins.py --check    # validate sources.json only, write nothing

Each skin becomes a square, metadata-free WebP (default 800x800, quality 82)
whose rose is centred and fills the frame; the app clips it to a circle.
Raw originals stay out of git (skins/raw/); sources.json records where each
came from, and skins.json is the generated index the app reads.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKINS = ROOT / "skins"
RAW = SKINS / "raw"
FIELDS = ("id", "name", "file", "credit", "license", "source")


def load_sources(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    seen: set[str] = set()
    for s in data["skins"]:
        missing = [f for f in FIELDS if not s.get(f)]
        if missing:
            raise ValueError(f"{s.get('id', '?')}: missing {', '.join(missing)}")
        if not s["id"].replace("-", "").isalnum() or s["id"] != s["id"].lower():
            raise ValueError(f"{s['id']}: id must be lowercase letters, digits and dashes")
        if s["id"] in seen:
            raise ValueError(f"duplicate id {s['id']}")
        seen.add(s["id"])
    return data


def open_source(path: Path, width: int):
    """Open a raster, or rasterize an SVG onto white, as RGB."""
    from PIL import Image
    if path.suffix.lower() == ".svg":
        import resvg_py
        png = bytes(resvg_py.svg_to_bytes(svg_path=str(path), width=width))
        im = Image.open(io.BytesIO(png)).convert("RGBA")
        flat = Image.new("RGBA", im.size, (255, 255, 255, 255))
        flat.alpha_composite(im)
        return flat.convert("RGB")
    Image.MAX_IMAGE_PIXELS = 100_000_000  # bound decompression-bomb risk
    return Image.open(path).convert("RGB")


def square(im, spec: dict):
    """Crop (or pad, for trimmed vector art) to a square around the rose."""
    from PIL import Image, ImageChops
    w, h = im.size
    if spec.get("trim"):
        bbox = ImageChops.difference(im, Image.new("RGB", im.size, (255, 255, 255))).getbbox()
        if bbox:
            im = im.crop(bbox)
            w, h = im.size
        side = round(max(w, h) * (1 + 2 * spec.get("margin", 0.0)))
        out = Image.new("RGB", (side, side), (255, 255, 255))
        out.paste(im, ((side - w) // 2, (side - h) // 2))
        return out
    cx, cy = spec.get("center", [0.5, 0.5])
    side = min(w, h) * spec.get("span", 1.0)
    left = min(max(cx * w - side / 2, 0), w - side)
    top = min(max(cy * h - side / 2, 0), h - side)
    return im.crop((round(left), round(top), round(left + side), round(top + side)))


def build(data: dict, only: set[str] | None) -> list[dict]:
    from PIL import Image
    size, quality = data.get("size", 800), data.get("quality", 82)
    index = []
    for s in data["skins"]:
        if only and s["id"] not in only:
            continue
        src = RAW / s["file"]
        if not src.is_file():
            print(f"  skip {s['id']}: {src} not found", file=sys.stderr)
            continue
        im = square(open_source(src, size * 2), s).resize((size, size), Image.LANCZOS)
        out = SKINS / f"{s['id']}.webp"
        im.save(out, "WEBP", quality=quality, method=6)   # no EXIF/ICC carried over
        print(f"  {s['id']:16} {out.stat().st_size // 1024:4d} KB")
        index.append({k: s[k] for k in FIELDS if k != "file"} | {"image": out.name})
    return index


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="validate sources.json and exit")
    ap.add_argument("--only", nargs="*", help="rebuild just these skin ids")
    args = ap.parse_args()
    data = load_sources(SKINS / "sources.json")
    if args.check:
        print(f"{len(data['skins'])} skins OK")
        return 0
    index = build(data, set(args.only) if args.only else None)
    if not args.only:
        (SKINS / "skins.json").write_text(
            json.dumps({"skins": index}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
