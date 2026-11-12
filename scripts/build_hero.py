#!/usr/bin/env python3
"""Turn a banner master (hero or footer) into the two README variants.

GitHub strips CSS from READMEs, so the rounded corners and the theme
fit live in the pixels:

    assets/<name>.png       light theme: rounded corners + faint gray rim
    assets/<name>-dark.png  dark theme: rounded corners, edges of the paper
                          settled a touch darker, thin flat rim

Dev-only tool, needs Pillow (pip install pillow). The cards workflow
does not run it.

Usage:
    python -I scripts/build_hero.py "path/to/Banner github.png"
    python -I scripts/build_hero.py assets/source/footer-original.png --name footer
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
SS = 4  # supersampling for anti-aliased corners and rims

LIGHT_RIM = (208, 215, 222)  # GitHub light border tone
DARK_RIM = (34, 34, 34)  # brand #222222
MAX_BYTES = 3_500_000


def rounded_mask(size, radius, inset=0):
    w, h = size
    m = Image.new("L", (w * SS, h * SS), 0)
    ImageDraw.Draw(m).rounded_rectangle(
        [inset * SS, inset * SS, w * SS - 1 - inset * SS, h * SS - 1 - inset * SS],
        radius=max(radius - inset, 0) * SS,
        fill=255,
    )
    return m.resize(size, Image.LANCZOS)


def build(src: Image.Image, rim_rgb, rim_px: int, settle: float) -> Image.Image:
    rgb = src.convert("RGB")
    w, h = rgb.size
    radius = round(w * 0.0225)
    shape = rounded_mask(rgb.size, radius)

    if settle:
        # darken only the outer band of the paper, flat falloff, no glow
        soft = rounded_mask(rgb.size, radius, inset=round(w * 0.02)).filter(ImageFilter.GaussianBlur(w * 0.025))
        shade = soft.point(lambda v: round(255 * (1 - settle * (1 - v / 255))))
        rgb = ImageChops.multiply(rgb, Image.merge("RGB", [shade] * 3))

    if rim_px:
        rim = ImageChops.subtract(shape, rounded_mask(rgb.size, radius, inset=rim_px))
        rgb = Image.composite(Image.new("RGB", rgb.size, rim_rgb), rgb, rim)

    out = rgb.convert("RGBA")
    out.putalpha(shape)
    return out


def save(img: Image.Image, path: Path):
    img.save(path, optimize=True, compress_level=9)
    if path.stat().st_size > MAX_BYTES:
        # fall back to a 256 color palette with alpha; halftone art survives it well
        img.quantize(256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.FLOYDSTEINBERG).save(path, optimize=True)
    print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size / 1e6:.2f} MB)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build <name>.png and <name>-dark.png from a banner master.")
    ap.add_argument("src", type=Path)
    ap.add_argument("--out", type=Path, default=ROOT / "assets")
    ap.add_argument("--name", default="hero", help="output base name (hero, footer)")
    args = ap.parse_args(argv)

    src = Image.open(args.src)
    w = src.size[0]
    rim = max(2, round(w / 846))  # about 1 css px at GitHub's README width
    save(build(src, LIGHT_RIM, rim, 0), args.out / f"{args.name}.png")
    save(build(src, DARK_RIM, rim, 0.10), args.out / f"{args.name}-dark.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
