"""本地照片占位图：纯标准库生成确定性 PNG（拆前/复装后对照用）。

不依赖 Pillow：手工构造 PNG（zlib 压缩的 RGB 扫描线）。
同一帘片编号 + 阶段生成的图像是确定的，便于对照页稳定展示。
"""
import hashlib
import os
import struct
import zlib

W, H = 240, 320

# 亚麻白 / 布艺蓝 / 暖灰 色系
PALETTE = [
    (250, 247, 240),  # 亚麻白
    (74, 111, 165),   # 布艺蓝
    (154, 149, 141),  # 暖灰
    (196, 188, 172),  # 浅麻
    (106, 138, 184),  # 浅蓝
    (128, 122, 112),  # 深灰
]


def _png(rgb_rows, w, h):
    """把 [[(r,g,b)...]...] 编码为 PNG 字节。"""
    raw = b"".join(
        b"\x00" + b"".join(struct.pack("BBB", *px) for px in row)
        for row in rgb_rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def _rng_for(key):
    seed = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")

    def rnd():
        nonlocal seed
        seed = (seed * 6364136223846793005 + 1442695040888963407) & (2**64 - 1)
        return seed >> 33
    return rnd


def render(key, kind):
    """生成一幅“窗帘照片”：竖向褶皱条纹 + 顶部挂杆 + 挂钩点。"""
    rnd = _rng_for(f"{key}:{kind}")
    base = PALETTE[rnd() % len(PALETTE)]
    accent = PALETTE[rnd() % len(PALETTE)]
    folds = 6 + rnd() % 5
    rows = []
    for y in range(H):
        row = []
        for x in range(W):
            if y < 14:  # 挂杆
                px = (90, 88, 84)
            elif y < 22 and x % (W // folds) < 5:  # 挂钩
                px = (60, 60, 60)
            else:
                wave = (x * folds * 3 // W + y // 40) % 2
                shade = 1.0 - 0.18 * wave
                c = base if (x // (W // folds)) % 2 == 0 else accent
                px = tuple(min(255, int(v * shade)) for v in c)
            row.append(px)
        rows.append(row)
    return _png(rows, W, H)


def _file(photo_dir, code, kind, ext=".png"):
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in code)
    return os.path.join(photo_dir, f"{safe}_{kind}{ext}")


def ensure_photo(photo_dir, code, kind):
    """若本地还没有该帘片该阶段的照片，则生成占位图。返回相对 URL 路径。"""
    path = _file(photo_dir, code, kind)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(render(code, kind))
    return photo_path(photo_dir, code, kind)


def photo_path(photo_dir, code, kind):
    """返回已存在照片的 URL 路径，不存在则返回 None。"""
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in code)
    prefix = f"{safe}_{kind}."
    if os.path.isdir(photo_dir):
        for name in sorted(os.listdir(photo_dir)):
            if name.startswith(prefix):
                return "/photos/" + name
    return None


def save_upload(photo_dir, code, kind, data, ext=".png"):
    """保存上传的照片字节，返回 URL 路径。"""
    ext = ext if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp") else ".png"
    path = _file(photo_dir, code, kind, ext)
    with open(path, "wb") as f:
        f.write(data)
    return "/photos/" + os.path.basename(path)
