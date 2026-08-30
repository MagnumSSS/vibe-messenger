#!/usr/bin/env python3
"""Generate VibeBunker PWA icons using only stdlib (zlib, struct)."""
import zlib, struct, math, os

OUT_DIR = os.path.join(os.path.dirname(__file__), '..', 'static')

# Dark theme colors (from THEME_PRESETS["dark"])
PANEL   = (0x16, 0x21, 0x3e)   # #16213e
ACCENT  = (0xe9, 0x45, 0x60)   # #e94560

def _png_chunk(chunk_type, data):
    c = chunk_type + data
    return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)

def _make_icon(size):
    """Render a size×size RGBA image: rounded square + accent circle."""
    pixels = bytearray()
    cx, cy = size / 2, size / 2
    radius = size * 0.22          # accent circle radius
    corner_r = size * 0.18        # rounded-rect corner radius
    margin = size * 0.06          # padding inside the icon

    for y in range(size):
        row = b'\x00'  # filter: None
        for x in range(size):
            # Check if inside rounded rectangle
            inside = False
            # Clamp to corner circles
            dx = max(0, max(x - (size - margin - corner_r), (margin + corner_r) - x))
            dy = max(0, max(y - (size - margin - corner_r), (margin + corner_r) - y))
            if dx == 0 and dy == 0:
                inside = True
            elif dx <= corner_r and dy <= corner_r:
                if math.sqrt(dx*dx + dy*dy) <= corner_r:
                    inside = True

            # Check if inside accent circle
            dist_center = math.sqrt((x - cx)**2 + (y - cy)**2)
            in_circle = dist_center <= radius

            if not inside:
                r, g, b, a = 0, 0, 0, 0
            elif in_circle:
                r, g, b = ACCENT
                a = 255
            else:
                r, g, b = PANEL
                a = 255
            pixels += struct.pack('BBBB', r, g, b, a)
        pixels += row

    # Build PNG
    sig = b'\x89PNG\r\n\x1a\n'
    ihdr = struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    compressed = zlib.compress(bytes(pixels), 9)
    return sig + _png_chunk(b'IHDR', ihdr) + _png_chunk(b'IDAT', compressed) + _png_chunk(b'IEND', b'')

if __name__ == '__main__':
    for sz in (192, 512):
        path = os.path.join(OUT_DIR, f'icon-{sz}.png')
        with open(path, 'wb') as f:
            f.write(_make_icon(sz))
        print(f'Wrote {path} ({sz}×{sz})')
