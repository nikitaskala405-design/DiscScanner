"""Рисует иконку приложения icon.ico (зелёный «радар» на чёрном фоне). Нужен Pillow."""

import os

from PIL import Image, ImageDraw, ImageFilter

S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((16, 16, S - 16, S - 16), radius=96, fill=(5, 12, 7, 255), outline=(0, 255, 102, 255), width=14)

glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
g = ImageDraw.Draw(glow)
c = S // 2
g.pieslice((96, 96, S - 96, S - 96), start=-90, end=-20, fill=(0, 255, 102, 110))
for r, w in ((160, 12), (110, 8), (60, 6)):
    g.ellipse((c - r, c - r, c + r, c + r), outline=(0, 255, 102, 255), width=w)
g.line((c, c, c + 115, c - 115), fill=(200, 255, 220, 255), width=10)
g.ellipse((c - 18, c - 18, c + 18, c + 18), fill=(0, 255, 102, 255))
g.ellipse((c + 70, c + 30, c + 96, c + 56), fill=(200, 255, 220, 255))
img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(10)))
img.alpha_composite(glow)

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("saved", out)
