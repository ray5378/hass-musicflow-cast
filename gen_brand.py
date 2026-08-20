import zlib, struct, os

W = H = 256
bg = (26, 26, 46)
accent = (233, 69, 96)
white = (255, 255, 255)

px = [bg] * (W * H)
cx, cy = W // 2, H // 2

# center circle
r = 92
for y in range(H):
    for x in range(W):
        if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
            px[y * W + x] = accent

# play triangle
def sign(a, b, c):
    return (a[0] - c[0]) * (b[1] - c[1]) - (b[0] - c[0]) * (a[1] - c[1])

def in_tri(x, y, p1, p2, p3):
    d1 = sign((x, y), p1, p2)
    d2 = sign((x, y), p2, p3)
    d3 = sign((x, y), p3, p1)
    neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (neg and pos)

p1 = (cx - 30, cy - 48)
p2 = (cx - 30, cy + 48)
p3 = (cx + 54, cy)
for y in range(H):
    for x in range(W):
        if in_tri(x, y, p1, p2, p3):
            px[y * W + x] = white

raw = bytearray()
for y in range(H):
    raw.append(0)
    for x in range(W):
        raw += bytes(px[y * W + x])

def chunk(typ, data):
    c = struct.pack(">I", len(data)) + typ + data
    crc = zlib.crc32(typ + data) & 0xFFFFFFFF
    return c + struct.pack(">I", crc)

png = bytearray(b"\x89PNG\r\n\x1a\n")
png += chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
png += chunk(b"IEND", b"")

os.makedirs("brand", exist_ok=True)
for name in ("icon.png", "logo.png"):
    with open(os.path.join("brand", name), "wb") as f:
        f.write(png)
print("wrote brand/icon.png + brand/logo.png, bytes:", len(png))
