"""生成 CTMD 的 .ico 图标（纯标准库，不需要 Pillow）。

用法：python tools/make_icon.py
产物：assets/ctmd.ico（64x64，PNG-in-ICO，Windows Vista+ 原生支持）
"""

import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "ctmd.ico"

SIZE = 64
BG_TOP = (47, 111, 235)      # #2f6feb
BG_BOT = (30, 64, 175)       # #1e40af
FG = (255, 255, 255)
RADIUS = 15.0

# 5x7 点阵里的字母 C
GLYPH = [
    "01110",
    "10001",
    "10000",
    "10000",
    "10000",
    "10001",
    "01110",
]
SCALE = 5
GW = len(GLYPH[0]) * SCALE
GH = len(GLYPH) * SCALE
GX = (SIZE - GW) // 2
GY = (SIZE - GH) // 2


def _round_alpha(x: float, y: float) -> float:
    """圆角矩形的覆盖率（做一点抗锯齿）。"""
    cx = min(max(x, RADIUS), SIZE - 1 - RADIUS)
    cy = min(max(y, RADIUS), SIZE - 1 - RADIUS)
    d = math.hypot(x - cx, y - cy)
    return max(0.0, min(1.0, RADIUS - d + 0.5))


def _in_glyph(x: int, y: int) -> bool:
    gx, gy = x - GX, y - GY
    if not (0 <= gx < GW and 0 <= gy < GH):
        return False
    return GLYPH[gy // SCALE][gx // SCALE] == "1"


def _rows():
    rows = []
    for y in range(SIZE):
        t = y / (SIZE - 1)
        bg = tuple(int(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * t) for i in range(3))
        row = []
        for x in range(SIZE):
            a = _round_alpha(x + 0.5, y + 0.5)
            if a <= 0:
                row.append((0, 0, 0, 0))
                continue
            if _in_glyph(x, y):
                row.append((*FG, int(255 * a)))
            else:
                row.append((*bg, int(255 * a)))
        rows.append(row)
    return rows


def png_bytes(rows) -> bytes:
    raw = b"".join(b"\x00" + b"".join(bytes(px) for px in row) for row in rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def main() -> None:
    png = png_bytes(_rows())
    header = struct.pack("<HHH", 0, 1, 1)                     # reserved, type=1(icon), count
    entry = struct.pack("<BBBBHHII", SIZE, SIZE, 0, 0, 1, 32, len(png), 6 + 16)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(header + entry + png)
    print(f"✓ {OUT}（{OUT.stat().st_size} 字节）")


if __name__ == "__main__":
    main()
