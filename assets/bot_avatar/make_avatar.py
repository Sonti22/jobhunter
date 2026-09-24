"""Аватар бота jobhunter: «радар вакансий». Рисуется в 2048 px, отдаётся 1024 и 640."""
import math
import sys

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

S = 2048
C = S / 2
OUT = sys.argv[1] if len(sys.argv) > 1 else "avatar.png"

yy, xx = np.mgrid[0:S, 0:S].astype(np.float32)
dx, dy = xx - C, yy - C
r = np.sqrt(dx * dx + dy * dy) / C                       # 0 в центре, 1 на краю вписанного круга
ang = (np.degrees(np.arctan2(dy, dx)) + 360.0) % 360.0    # 0 = вправо, по часовой (ось y вниз)


def rgb(h):
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float32)


# 1. Фон: глубокий фиолетовый к почти чёрному, плюс два туманных свечения.
inner, outer = rgb("#23135c"), rgb("#04050d")
t = np.clip(r / 1.15, 0, 1)[..., None]
img = inner * (1 - t) ** 1.4 + outer * (1 - (1 - t) ** 1.4)
for cx, cy, rad, col, k in ((0.35, 0.28, 0.55, "#ff2e88", 0.30), (0.72, 0.78, 0.6, "#00e5ff", 0.22)):
    d = np.sqrt((xx / S - cx) ** 2 + (yy / S - cy) ** 2) / rad
    img += rgb(col) * (np.exp(-d * d * 3.0) * k)[..., None]

# 2. Луч радара: ведущая кромка на 318° (вверх-вправо), шлейф 75° против хода.
lead = 318.0
behind = (lead - ang) % 360.0
wedge = np.clip(1 - behind / 75.0, 0, 1) ** 2.2 * (behind <= 75)
wedge *= np.clip(1 - r, 0, 1) ** 0.35 * (r < 0.96)
img += rgb("#00e5ff") * (wedge * 0.55)[..., None]
edge = np.exp(-(behind / 1.6) ** 2) * (r < 0.96) * np.clip(1.1 - r, 0, 1)
img += rgb("#b8fbff") * (edge * 0.9)[..., None]

base = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")


def glow_layer(draw_fn, blur, strength=1.0):
    """Слой с фигурами + его размытая копия: неон."""
    layer = Image.new("RGB", (S, S), (0, 0, 0))
    draw_fn(ImageDraw.Draw(layer))
    halo = layer.filter(ImageFilter.GaussianBlur(blur))
    if strength != 1.0:
        halo = Image.eval(halo, lambda v: min(255, int(v * strength)))
    return ImageChops.add(layer, halo)


def ring(d, rr, width, col):
    d.ellipse([C - rr, C - rr, C + rr, C + rr], outline=col, width=width)


# 3. Кольца, перекрестье, деления.
def grid(d):
    for k, a in ((0.30, 70), (0.50, 60), (0.70, 50), (0.90, 70)):
        ring(d, C * k, 5, (0, int(2.2 * a), int(2.6 * a)))
    for i in range(72):
        a = math.radians(i * 5)
        l1 = C * (0.86 if i % 3 else 0.82)
        d.line([C + l1 * math.cos(a), C + l1 * math.sin(a),
                C + C * 0.90 * math.cos(a), C + C * 0.90 * math.sin(a)],
               fill=(0, 150, 180), width=4)
    d.line([C - C * 0.9, C, C + C * 0.9, C], fill=(0, 90, 120), width=4)
    d.line([C, C - C * 0.9, C, C + C * 0.9], fill=(0, 90, 120), width=4)


base = ImageChops.add(base, glow_layer(grid, 10, 1.2))


# 4. Метки-вакансии в шлейфе луча и одна «захваченная» на кромке.
def blips(d):
    for deg, rr, size, bright in ((300, 0.62, 16, 200), (285, 0.40, 13, 150), (262, 0.76, 11, 110),
                                  (330, 0.34, 12, 170), (250, 0.55, 9, 90)):
        a = math.radians(deg)
        x, y = C + C * rr * math.cos(a), C + C * rr * math.sin(a)
        d.ellipse([x - size, y - size, x + size, y + size], fill=(bright // 3, bright, bright))


base = ImageChops.add(base, glow_layer(blips, 18, 1.6))

tx, ty = C + C * 0.74 * math.cos(math.radians(314)), C + C * 0.74 * math.sin(math.radians(314))


def target(d):
    d.ellipse([tx - 20, ty - 20, tx + 20, ty + 20], fill=(255, 235, 245))
    s, g, w = 62, 26, 8                              # скобки захвата цели
    col = (255, 46, 136)
    for sx in (-1, 1):
        for sy in (-1, 1):
            cx, cy = tx + sx * s, ty + sy * s
            d.line([cx, cy, cx - sx * g, cy], fill=col, width=w)
            d.line([cx, cy, cx, cy - sy * g], fill=col, width=w)


base = ImageChops.add(base, glow_layer(target, 22, 1.8))

# 5. Портфель в центре: градиент розовый → оранжевый, белый блик, неоновый ореол.
bw, bh = S * 0.40, S * 0.29
bx0, by0 = C - bw / 2, C - bh / 2 + S * 0.035
body = Image.new("L", (S, S), 0)
bd = ImageDraw.Draw(body)
bd.rounded_rectangle([bx0, by0, bx0 + bw, by0 + bh], radius=S * 0.045, fill=255)
hw, hh, ht = bw * 0.36, bh * 0.30, S * 0.024                  # ручка
bd.rounded_rectangle([C - hw / 2, by0 - hh, C + hw / 2, by0 + ht], radius=S * 0.03, fill=255)
bd.rounded_rectangle([C - hw / 2 + ht, by0 - hh + ht, C + hw / 2 - ht, by0 + ht * 0.2],
                     radius=S * 0.012, fill=0)

grad_t = np.clip((xx - bx0) / bw * 0.6 + (yy - by0) / bh * 0.4, 0, 1)[..., None]
fill = rgb("#ff2e88") * (1 - grad_t) + rgb("#ff9a1f") * grad_t
# объём: к низу темнее, сверху глянцевая полоса
vert = np.clip((yy - by0) / bh, 0, 1)[..., None]
fill = fill * (1.0 - 0.28 * vert ** 1.6)
gloss = np.clip(1 - np.abs((yy - (by0 + bh * 0.16)) / (bh * 0.13)), 0, 1) ** 1.5
fill = fill + (255 - fill) * (gloss * 0.32)[..., None]
fill_img = Image.fromarray(np.clip(fill, 0, 255).astype(np.uint8), "RGB")

halo = Image.new("RGB", (S, S), (0, 0, 0))
halo.paste(Image.new("RGB", (S, S), (255, 60, 140)), mask=body)
base = ImageChops.add(base, Image.eval(halo.filter(ImageFilter.GaussianBlur(60)), lambda v: int(v * 0.75)))
base.paste(fill_img, mask=body)

# полоса-застёжка и замочек
detail = ImageDraw.Draw(base)
band_y = by0 + bh * 0.40
detail.rounded_rectangle([bx0 + 6, band_y - 10, bx0 + bw - 6, band_y + 10], radius=10, fill=(120, 16, 70))
detail.rounded_rectangle([C - 46, band_y - 34, C + 46, band_y + 34], radius=16,
                         fill=(255, 240, 225), outline=(120, 16, 70), width=8)

# белый контур-блик по верхней кромке портфеля
hl = Image.new("L", (S, S), 0)
ImageDraw.Draw(hl).rounded_rectangle([bx0, by0, bx0 + bw, by0 + bh], radius=S * 0.045,
                                     outline=255, width=10)
top_mask = Image.fromarray((np.clip(1.4 - (yy - by0) / (bh * 0.55), 0, 1) * 255).astype(np.uint8), "L")
hl = ImageChops.multiply(hl, top_mask)
base.paste(Image.new("RGB", (S, S), (255, 245, 250)), mask=hl.filter(ImageFilter.GaussianBlur(1.5)))

# 6. Внешнее кольцо-рамка: переход бирюза → розовый по кругу.
# косинус от угла — переход без шва: розовый сверху-слева, бирюзовый снизу-справа
frame_t = ((1 - np.cos(np.radians(ang - 225.0))) / 2)[..., None]
frame_col = rgb("#00e5ff") * (1 - frame_t) + rgb("#ff2e88") * frame_t
frame_mask = ((r > 0.955) & (r < 0.985)).astype(np.float32)
frame = Image.fromarray(np.clip(frame_col * frame_mask[..., None], 0, 255).astype(np.uint8), "RGB")
base = ImageChops.add(base, ImageChops.add(frame, frame.filter(ImageFilter.GaussianBlur(14))))

base = base.resize((1024, 1024), Image.LANCZOS)
base.save(OUT)
base.resize((640, 640), Image.LANCZOS).save(OUT.replace(".png", "_640.png"))
print("saved", OUT)
