"""Draws TradingView-style test charts with a position tool on them."""

import random

from PIL import Image, ImageDraw, ImageFont

W, H, AXIS, LOW, HIGH = 1400, 800, 110, 4135.0, 4195.0
CURRENT = 4170.60


def _font(size):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def draw_chart(path, entry, sl, tp, theme="dark"):
    """Gold chart ending at 4170.60, with one position tool: red box on the
    SL side of the entry, green on the TP side, price labels on the right.
    A tool whose entry is on the current price starts at the last candle,
    as a market execution does."""
    font, small, big = _font(15), _font(13), _font(18)
    y = lambda p: int(40 + (HIGH - p) / (HIGH - LOW) * (H - 80))
    bg, grid, txt = (((19, 23, 34), (36, 41, 56), (178, 181, 190)) if theme == "dark"
                     else ((255, 255, 255), (238, 240, 244), (60, 60, 60)))
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img, "RGBA")
    for p in range(4140, 4196, 5):
        d.line([(0, y(p)), (W - AXIS, y(p))], fill=grid)
        d.text((W - AXIS + 10, y(p) - 8), f"{p:.2f}", font=small, fill=txt)

    n = 95
    x0 = 20 + (n - 1) * 12 if abs(entry - CURRENT) < 0.01 else 20 + 88 * 12
    x1 = W - AXIS - 10
    d.rectangle([x0, y(max(entry, sl)), x1, y(min(entry, sl))], fill=(242, 54, 69, 70))
    d.rectangle([x0, y(max(entry, tp)), x1, y(min(entry, tp))], fill=(8, 153, 129, 70))
    d.line([(x0, y(entry)), (x1, y(entry))], fill=(130, 130, 130), width=2)
    for value, colour in ((sl, (242, 54, 69)), (entry, (120, 123, 134)), (tp, (8, 153, 129))):
        label = f"{value:.2f}"
        width = d.textlength(label, font=small)
        d.rectangle([x1 - width - 12, y(value) - 9, x1 - 2, y(value) + 9], fill=colour)
        d.text((x1 - width - 7, y(value) - 8), label, font=small, fill="white")

    random.seed(7)
    price = 4142.0
    for i in range(n):
        drift = 0.45 if i < 70 else (CURRENT - 4174.0) / 25
        o = price
        c = o + drift + random.uniform(-2.0, 2.0)
        if i >= n - 3:
            c = CURRENT + (n - 1 - i) * 0.4
        h = max(o, c) + random.uniform(0, 1.4)
        lo = min(o, c) - random.uniform(0, 1.4)
        x = 20 + i * 12
        colour = (8, 153, 129) if c >= o else (242, 54, 69)
        d.line([(x + 4, y(h)), (x + 4, y(lo))], fill=colour)
        d.rectangle([x, y(max(o, c)), x + 8, y(min(o, c)) + 1], fill=colour)
        price = c
    d.line([(0, y(CURRENT)), (W - AXIS, y(CURRENT))], fill=(242, 54, 69))
    d.rectangle([W - AXIS, y(CURRENT) - 11, W, y(CURRENT) + 11], fill=(242, 54, 69))
    d.text((W - AXIS + 10, y(CURRENT) - 9), f"{CURRENT:.2f}", font=font, fill="white")
    d.text((14, 10), "Gold Spot / U.S. Dollar · 5 · OANDA", font=big, fill=txt)
    img.save(path)
