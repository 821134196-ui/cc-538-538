"""巡查照片：纯标准库生成/解析 PNG（无需 Pillow）。

- generate_tree_photo：合成一张亮度充足的树木巡查照片（天空+树冠+树干+少量花色）
- png_brightness   ：解析 PNG 像素并计算平均感知亮度 0..1（服务端二次校验）
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path


# --------------------------------------------------------------- PNG 编码

def _png_chunk(tag: bytes, data: bytes) -> bytes:
    chunk = tag + data
    return struct.pack(">I", len(data)) + chunk + struct.pack(">I", zlib.crc32(chunk))


def write_png(path: Path, width: int, height: int, rows: list[bytearray]) -> None:
    """rows：每像素 RGB 三字节的二维数组。"""
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    png = (b"\x89PNG\r\n\x1a\n"
           + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + _png_chunk(b"IDAT", zlib.compress(raw, 6))
           + _png_chunk(b"IEND", b""))
    path.write_bytes(png)


# --------------------------------------------------------------- PNG 解码

def read_png(path: Path) -> tuple[int, int, list[bytes]]:
    """支持 8-bit RGB/RGBA、非隔行 PNG。返回 (w,h,[每行 BPP*w 字节])。"""
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG 文件")
    pos, width, height, bit_depth, color_type = 8, 0, 0, 0, 0
    idat = b""
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", body[:10])
            if bit_depth != 8 or color_type not in (2, 6):
                raise ValueError("仅支持 8-bit RGB/RGBA PNG")
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
        pos += 12 + length
    bpp = 4 if color_type == 6 else 3
    raw = zlib.decompress(idat)
    stride = width * bpp
    rows: list[bytes] = []
    prev = bytearray(stride)
    i = 0
    for _ in range(height):
        filt = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if filt == 1:
                line[x] = (line[x] + a) & 255
            elif filt == 2:
                line[x] = (line[x] + b) & 255
            elif filt == 3:
                line[x] = (line[x] + ((a + b) >> 1)) & 255
            elif filt == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 255
            elif filt != 0:
                raise ValueError("不支持的 PNG 滤镜")
        rows.append(bytes(line))
        prev = line
    return width, height, rows


def png_brightness(path: Path) -> float:
    """平均感知亮度（Rec.601），返回 0..1。"""
    w, h, rows = read_png(path)
    bpp = len(rows[0]) // w if w else 3
    total, n = 0.0, 0
    for y, line in enumerate(rows):
        if y % 3:                       # 抽样 1/3 行，足够判定
            continue
        for x in range(0, w):
            i = x * bpp
            r, g, b = line[i], line[i + 1], line[i + 2]
            total += 0.299 * r + 0.587 * g + 0.114 * b
            n += 1
    return total / n / 255 if n else 0.0


# --------------------------------------------------------------- 照片合成

def _blend(rows, x0, y0, x1, y1, color, alpha, w, h):
    r0, g0, b0 = color
    for y in range(max(0, y0), min(h, y1)):
        row = rows[y]
        for x in range(max(0, x0), min(w, x1)):
            i = x * 3
            row[i] = int(row[i] * (1 - alpha) + r0 * alpha)
            row[i + 1] = int(row[i + 1] * (1 - alpha) + g0 * alpha)
            row[i + 2] = int(row[i + 2] * (1 - alpha) + b0 * alpha)


def _disc(rows, cx, cy, rad, color, alpha, w, h):
    r0, g0, b0 = color
    for y in range(max(0, int(cy - rad)), min(h, int(cy + rad) + 1)):
        for x in range(max(0, int(cx - rad)), min(w, int(cx + rad) + 1)):
            d = math.hypot(x - cx, y - cy)
            if d <= rad:
                # 边缘轻微羽化，避免生硬
                aa = alpha * min(1.0, (rad - d) + 0.5)
                i = x * 3
                row = rows[y]
                row[i] = int(row[i] * (1 - aa) + r0 * aa)
                row[i + 1] = int(row[i + 1] * (1 - aa) + g0 * aa)
                row[i + 2] = int(row[i + 2] * (1 - aa) + b0 * aa)


def generate_tree_photo(path: Path, seed: int, label: str = "",
                        size: tuple[int, int] = (320, 240)) -> float:
    """生成一张明亮、可辨识的树木照片；返回实际平均亮度。"""
    w, h = size
    # 明亮天空渐变（白 → 淡森林绿）
    rows = []
    for y in range(h):
        t = y / h
        row = bytearray(w * 3)
        base = (int(244 - 12 * t), int(250 - 18 * t), int(246 - 26 * t))
        for x in range(w):
            i = x * 3
            row[i], row[i + 1], row[i + 2] = base
        rows.append(row)
    # 草地
    _blend(rows, 0, int(h * 0.72), w, h, (203, 226, 197), 0.9, w, h)
    # 步道（浅色带，保持亮度）
    _blend(rows, int(w * 0.62), int(h * 0.72), int(w * 0.72), h, (232, 226, 205), 0.95, w, h)

    # 确定性"随机"
    state = seed

    def rnd():
        nonlocal state
        state = (state * 1103515245 + 12345) & 0x7fffffff
        return state / 0x7fffffff

    # 树干
    tx = int(w * (0.36 + 0.1 * rnd()))
    _blend(rows, tx - 7, int(h * 0.4), tx + 7, int(h * 0.78), (124, 92, 58), 0.95, w, h)
    # 树冠：森林绿层次
    for cy_f, cx_f, rad_f, tone in (
        (0.30, 0.00, 0.20, (46, 109, 69)),
        (0.26, -0.09, 0.15, (58, 125, 80)),
        (0.28, 0.10, 0.14, (38, 96, 60)),
        (0.38, 0.02, 0.13, (70, 140, 92)),
    ):
        cx = tx + int(w * cx_f + 12 * (rnd() - 0.5))
        cy = int(h * cy_f + 8 * (rnd() - 0.5))
        _disc(rows, cx, cy, int(w * rad_f), tone, 0.92, w, h)
    # 少量花色点缀（亮粉/白），且占比很小不影响整体辨识
    for _ in range(7):
        cx = tx + int(w * (0.18 * (rnd() - 0.5)))
        cy = int(h * (0.24 + 0.16 * rnd()))
        col = (226, 122, 153) if rnd() > 0.5 else (250, 248, 240)
        _disc(rows, cx, cy, 4, col, 0.95, w, h)

    write_png(path, w, h, rows)
    b = png_brightness(path)
    if label:
        path.with_suffix(path.suffix + ".txt").write_text(
            label, encoding="utf-8")
    return b
