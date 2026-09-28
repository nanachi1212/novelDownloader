"""Generate the small set of local MSIX PNG assets without third-party files."""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path


def png(path: Path, width: int, height: int) -> None:
    pixels = bytearray()
    for y in range(height):
        pixels.append(0)
        for x in range(width):
            # A simple book mark made only from local vector-like geometry.
            margin_x = width // 5
            margin_y = height // 5
            book = margin_x <= x < width - margin_x and margin_y <= y < height - margin_y
            spine = width // 2 - max(1, width // 40) <= x <= width // 2 + max(1, width // 40)
            edge = y >= height * 3 // 4 and x >= width // 3 and x < width * 2 // 3
            if book and not spine:
                color = (245, 247, 250, 255)
            elif edge:
                color = (116, 180, 214, 255)
            else:
                color = (30, 58, 95, 255)
            pixels.extend(color)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data +
                struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    raw = zlib.compress(bytes(pixels), 9)
    data = (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", raw) + chunk(b"IEND", b""))
    path.write_bytes(data)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: generate_assets.py OUTPUT_DIR", file=sys.stderr)
        return 2
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    for name, width, height in (("StoreLogo.png", 50, 50),
                                ("Square44x44Logo.png", 44, 44),
                                ("Square150x150Logo.png", 150, 150),
                                ("Wide310x150Logo.png", 310, 150)):
        png(output / name, width, height)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
