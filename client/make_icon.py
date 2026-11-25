"""Draw the Meeting Captions app icon (used by the exe, its windows, shortcuts and the tray).

    python client/make_icon.py      -> client/assets/icon.ico (16-256 px) and icon.png (256 px)

Design: Google-blue rounded square, two white speech bubbles (the dashboard's "forum" symbol) with
caption lines in the front bubble. Drawn at 4x and downsampled so small sizes stay crisp.
"""
from pathlib import Path

from PIL import Image, ImageDraw

BLUE, WHITE, LINE = (11, 87, 208, 255), (255, 255, 255, 255), (11, 87, 208, 255)
OUT = Path(__file__).parent / "assets"


def draw(size: int = 1024) -> Image.Image:
    s = size / 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(56 * s), fill=BLUE)
    # back bubble (top right, translucent white)
    d.rounded_rectangle([96 * s, 52 * s, 212 * s, 140 * s], radius=int(22 * s), fill=(255, 255, 255, 120))
    d.polygon([(176 * s, 138 * s), (204 * s, 168 * s), (196 * s, 136 * s)], fill=(255, 255, 255, 120))
    # front bubble (bottom left, white) with a tail
    d.rounded_rectangle([40 * s, 92 * s, 180 * s, 196 * s], radius=int(24 * s), fill=WHITE)
    d.polygon([(62 * s, 192 * s), (52 * s, 226 * s), (96 * s, 194 * s)], fill=WHITE)
    # caption lines
    for i, w in enumerate((104, 78)):
        y = (124 + i * 34) * s
        d.rounded_rectangle([62 * s, y, (62 + w) * s, y + 16 * s], radius=int(8 * s), fill=LINE)
    return img


def main() -> None:
    OUT.mkdir(exist_ok=True)
    big = draw()
    big.resize((256, 256), Image.LANCZOS).save(OUT / "icon.png")
    big.save(OUT / "icon.ico", sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", OUT / "icon.ico", "and", OUT / "icon.png")


if __name__ == "__main__":
    main()
