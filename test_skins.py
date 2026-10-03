"""Checks on the shipped dial skins (skins/*.webp, skins.json, sources.json).

Stdlib only: the WebP header is read directly so the app's tests don't need
Pillow. Rebuilding the skins is tools/make_skins.py; see docs/skins.md.
"""
from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

import rt21_web as app

SKINS = Path(__file__).resolve().parent / "skins"
SIZE = 800
MAX_BYTES = 300 * 1024          # per-skin budget: the repo carries these forever


def webp_info(data: bytes) -> tuple[int, int, bytes]:
    """(width, height, fourcc) of a simple lossy/lossless WebP; ValueError otherwise."""
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("not a WebP")
    fourcc = data[12:16]
    if fourcc == b"VP8 ":
        w, h = struct.unpack("<HH", data[26:30])
        return w & 0x3FFF, h & 0x3FFF, fourcc
    if fourcc == b"VP8L":
        bits = struct.unpack("<I", data[21:25])[0]
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1, fourcc
    raise ValueError(f"unexpected chunk {fourcc!r}")   # VP8X means metadata/animation


class ShippedSkinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = json.loads((SKINS / "skins.json").read_text(encoding="utf-8"))["skins"]

    def test_index_matches_files(self) -> None:
        ids = [s["id"] for s in self.index]
        self.assertEqual(len(ids), len(set(ids)))
        on_disk = {p.stem for p in SKINS.glob("*.webp")}
        self.assertEqual(on_disk, set(ids), "skins.json and *.webp disagree; rerun make_skins.py")
        self.assertEqual({s["id"] for s in app.load_skins()}, set(ids))

    def test_every_skin_is_credited(self) -> None:
        for s in self.index:
            for key in ("name", "credit", "license", "source"):
                self.assertTrue(s.get(key), f"{s['id']}: missing {key}")

    def test_images_are_square_small_and_metadata_free(self) -> None:
        for s in self.index:
            data = (SKINS / s["image"]).read_bytes()
            w, h, _ = webp_info(data)          # raises on VP8X, i.e. EXIF/ICC/XMP present
            self.assertEqual((w, h), (SIZE, SIZE), s["id"])
            self.assertLessEqual(len(data), MAX_BYTES, s["id"])
            for tag in (b"EXIF", b"XMP ", b"ICCP"):
                self.assertNotIn(tag, data, f"{s['id']} carries {tag!r}")

    def test_sources_json_is_valid_and_covers_the_index(self) -> None:
        sys.path.insert(0, str(Path(__file__).parent / "tools"))
        import make_skins
        data = make_skins.load_sources(SKINS / "sources.json")
        self.assertEqual({s["id"] for s in data["skins"]}, {s["id"] for s in self.index})


class LoadSkinsTests(unittest.TestCase):
    def test_missing_or_broken_index(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(app.load_skins(Path(d)), [])
            (Path(d) / "skins.json").write_text("{not json")
            self.assertEqual(app.load_skins(Path(d)), [])

    def test_hostile_entries_are_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "good.webp").write_bytes(b"x")
            (root / "orphan.webp").write_bytes(b"x")
            entries = [
                {"id": "good", "image": "good.webp"},
                {"id": "gone", "image": "gone.webp"},               # file missing
                {"id": "../evil", "image": "../evil.webp"},         # bad id
                {"id": "good2", "image": "../good.webp"},           # image != id.webp
                {"image": "x.webp"}, "junk",
            ]
            (root / "skins.json").write_text(json.dumps({"skins": entries}))
            self.assertEqual([s["id"] for s in app.load_skins(root)], ["good"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
