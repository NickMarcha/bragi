"""Regenerate every icon from docs/brand/bragi-logo.png.

    docker run --rm -v "$PWD":/w -w /w python:3.13-slim \
        sh -c 'pip install -q pillow && python dev/brand.py'

The source is the logo on its dark square. The mark is the same drawing with
that background keyed out, for places that have their own background.
"""
from collections import deque
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'docs/brand/bragi-logo.png'
BACKGROUND = (0x11, 0x13, 0x17)
OUTLINE = (0xd6, 0xfa, 0xf0)
ANDROID_RES = ROOT / 'android/app/src/main/res'
DENSITIES = {'mdpi': 1, 'hdpi': 1.5, 'xhdpi': 2, 'xxhdpi': 3, 'xxxhdpi': 4}


def mark(logo: Image.Image) -> Image.Image:
    """The logo without its square: flood the dark background in from the edges."""
    rgb = logo.convert('RGB')
    w, h = rgb.size
    px = rgb.load()
    out = rgb.convert('RGBA')
    opx = out.load()

    def distance(p):
        return max(abs(a - b) for a, b in zip(p, BACKGROUND))

    seen = bytearray(w * h)
    queue = deque((x, y) for x in range(w) for y in (0, h - 1))
    queue.extend((x, y) for y in range(h) for x in (0, w - 1))
    while queue:
        x, y = queue.popleft()
        if not (0 <= x < w and 0 <= y < h) or seen[y * w + x]:
            continue
        seen[y * w + x] = 1
        d = distance(px[x, y])
        if d > 60:
            continue
        # The background is noisy up to about 15 levels; above that, the anti-aliased
        # edge between background and outline becomes partial alpha.
        opx[x, y] = (*OUTLINE, max(0, d - 15) * 255 // 45)
        queue.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))
    box = out.getbbox()
    side = max(box[2] - box[0], box[3] - box[1])
    square = Image.new('RGBA', (side, side))
    square.paste(out.crop(box), ((side - (box[2] - box[0])) // 2, (side - (box[3] - box[1])) // 2))
    return square


def fit(image: Image.Image, size: int, scale: float = 1.0, background=None) -> Image.Image:
    canvas = Image.new('RGBA', (size, size), (*background, 255) if background else (0, 0, 0, 0))
    inner = max(1, round(size * scale))
    small = image.resize((inner, inner), Image.LANCZOS)
    canvas.alpha_composite(small, ((size - inner) // 2, (size - inner) // 2))
    return canvas


def silhouette(image: Image.Image) -> Image.Image:
    """White shape for Android's status bar and themed icons, which use alpha only."""
    alpha = image.getchannel('A')
    white = Image.new('RGBA', image.size, (255, 255, 255, 0))
    white.putalpha(alpha)
    return white


def save(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, optimize=True)
    print(path.relative_to(ROOT))


def main() -> None:
    logo = Image.open(SOURCE).convert('RGBA')
    m = mark(logo)
    save(m, ROOT / 'docs/brand/bragi-mark.png')

    # Web: favicon and header mark on the dashboard's own background,
    # touch icon with the square because iOS and Android fill transparency.
    static = ROOT / 'app/static'
    save(fit(m, 64), static / 'favicon.png')
    save(fit(m, 96), static / 'logo.png')
    save(logo.resize((180, 180), Image.LANCZOS), static / 'apple-touch-icon.png')

    # Android launcher (minSdk 29, so always adaptive): the foreground keeps the
    # mark inside the 66 dp safe zone of the 108 dp layer.
    for name, d in DENSITIES.items():
        save(fit(m, round(108 * d), 0.6), ANDROID_RES / f'mipmap-{name}/ic_launcher_foreground.png')
        save(fit(silhouette(m), round(108 * d), 0.6), ANDROID_RES / f'mipmap-{name}/ic_launcher_monochrome.png')
        save(fit(silhouette(m), round(24 * d), 0.92), ANDROID_RES / f'drawable-{name}/ic_stat_bragi.png')
    save(fit(m, 192), ANDROID_RES / 'drawable-nodpi/bragi_mark.png')

    # Desktop tray: colour when the link is on, grey when off, a red badge on errors.
    assets = ROOT / 'client/src/Bragi.Client/Assets'
    enabled = fit(m, 64)
    save(enabled, assets / 'tray-enabled.png')
    grey = enabled.convert('LA').convert('RGBA')
    grey.putalpha(enabled.getchannel('A').point(lambda a: a * 3 // 5))
    save(grey, assets / 'tray-disabled.png')
    error = enabled.copy()
    ImageDraw.Draw(error).ellipse((38, 38, 63, 63), fill=(0xd0, 0x52, 0x5a, 255), outline=(*BACKGROUND, 255), width=3)
    save(error, assets / 'tray-error.png')


if __name__ == '__main__':
    main()
