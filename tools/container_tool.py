# -*- coding: utf-8 -*-
"""
container_tool.py — 16 位主机 ROM 自定义图形容器 解码 / 渲染工具
================================================================
支持两种容器：
  容器 1：RLE + 可选 nibble 重映射（本工具主要对象）
  容器 2：原始拷贝变体（magic + u16 长度 + 数据）

格式（逆向自 ROM 内 68K 解码器，地址见 docs/02-container-format.md）:
  容器 1 = 4 字节魔数 + 控制字节流
      c == 0x00        -> 结束
      c in 1..0x7F     -> 从流中读取 c 个像素字节（每字节 = 2 个 4bpp 像素）
      c in 0x80..0xFF  -> 重复上一个像素字节 (c & 0x7F) + 1 次
      可选 nibble 重置换（入口 d5=1 时）: 0x7->0xA, 0xA->0xC, 0xC->0x7
  输出: 4 字节 = 1 行(8 像素), 32 字节 = 1 个 8x8 tile，线性写入 VRAM

脱敏说明:
  容器魔数是 4 字节 ASCII，其实际取值与本项目分析对象相关，已从本仓库移除。
  使用前请通过 --magic1 / --magic2 传入目标 ROM 的十六进制魔数（8 位十六进制字符）。

用法:
  python container_tool.py list      --rom <rom> --magic1 <HEX8>
  python container_tool.py decode    --rom <rom> --magic1 <HEX8> --offset 0x0DFE54 [--out b.bin]
  python container_tool.py render    --rom <rom> --magic1 <HEX8> --offset 0x0DFE54 [--out b.png]
  python container_tool.py renderall --rom <rom> --magic1 <HEX8> --outdir outdir
  python container_tool.py magic2    --rom <rom> --magic2 <HEX8> --offset 0x34A150 [--out b.bin]
"""
import argparse
import os
import re
import sys

# ---- 占位符：请填入目标 ROM 的容器魔数（8 位十六进制 = 4 字节 ASCII）----
# 留空即表示必须通过命令行传入；本仓库已脱敏，不提供实际取值。
DEFAULT_MAGIC1 = None
DEFAULT_MAGIC2 = None

# 代码中作为立即数出现、并非真实资源的匹配位置（结构信息，跳过即可）
CODE_IMMEDIATE_OFFSETS = (0x113, 0x445BC, 0x44698)

NIBBLE_MAP = {7: 10, 10: 12, 12: 7}


def parse_magic(text):
    s = text.strip().lower().replace("0x", "").replace(" ", "")
    if len(s) != 8 or any(c not in "0123456789abcdef" for c in s):
        raise argparse.ArgumentTypeError("魔数需为 8 位十六进制字符（4 字节）")
    if s == "00000000":
        raise argparse.ArgumentTypeError(
            "魔数还是占位值。本仓库已脱敏，请传入你自己的值（8 位十六进制）")
    return bytes.fromhex(s)


def need_magic(value, which):
    if value is None:
        print("!! 缺少 --%s。本仓库已脱敏，需传入目标 ROM 的容器魔数（8 位十六进制）" % which)
        sys.exit(2)
    return value


def nib_remap(x):
    return ((NIBBLE_MAP.get(x >> 4, x >> 4) << 4) | NIBBLE_MAP.get(x & 0x0F, x & 0x0F))


def container1_decode(data, off, transform=True):
    """off 指向魔数首字节；返回 (解码后的 4bpp 字节流, 消耗的源字节数)"""
    i = off + 4
    out = bytearray()
    n = len(data)
    guard = 0
    while True:
        guard += 1
        if guard > 10_000_000 or i >= n:
            break
        c = data[i]
        i += 1
        if c == 0:
            break
        if c < 0x80:
            for _ in range(c):
                if i >= n:
                    return bytes(out), i - off
                v = data[i]
                i += 1
                out.append(nib_remap(v) if transform else v)
        else:
            k = (c & 0x7F) + 1
            v = out[-1] if out else 0
            out += bytes([v]) * k
    return bytes(out), i - off


def magic2_raw_decode(data, off):
    """容器 2 原始拷贝变体：返回 (原始数据, 消耗的源字节数)"""
    ln = int.from_bytes(data[off + 4:off + 6], "big")
    body = ln & ~3
    return data[off + 6:off + 6 + body], 6 + body


def find_blobs(data, magic):
    return [m.start() for m in re.finditer(re.escape(magic), data)
            if m.start() not in CODE_IMMEDIATE_OFFSETS]


def render_tiles(dec, scale=3, wrap=16, bg=(0, 0, 0)):
    from PIL import Image
    nt = len(dec) // 32
    cols = max(min(nt, wrap), 1)
    rows = max((nt + wrap - 1) // wrap, 1)
    tw, th = 8 * scale, 8 * scale
    img = Image.new("RGB", (cols * tw, rows * th), bg)
    px = img.load()
    for t in range(nt):
        cx, cy = t % wrap, t // wrap
        tb = dec[t * 32:t * 32 + 32]
        for r in range(8):
            row = tb[r * 4:r * 4 + 4]
            for x in range(8):
                by = row[x // 2]
                v = (by >> 4) if x % 2 == 0 else (by & 0xF)
                g = v * 17
                for dy in range(scale):
                    for dx in range(scale):
                        px[cx * tw + x * scale + dx, cy * th + r * scale + dy] = (g, g, g)
    return img, nt


def main():
    ap = argparse.ArgumentParser(description="ROM 自定义图形容器 解码/渲染工具")
    ap.add_argument("command", choices=["list", "decode", "render", "renderall", "magic2"])
    ap.add_argument("--rom", required=True, help="ROM 文件路径")
    ap.add_argument("--magic1", type=parse_magic, default=DEFAULT_MAGIC1,
                    help="容器 1 魔数（8 位十六进制）")
    ap.add_argument("--magic2", type=parse_magic, default=DEFAULT_MAGIC2,
                    help="容器 2 魔数（8 位十六进制）")
    ap.add_argument("--offset", help="资源偏移，如 0x0DFE54")
    ap.add_argument("--out", help="输出文件路径")
    ap.add_argument("--outdir", help="renderall 的输出目录")
    ap.add_argument("--scale", type=int, default=3, help="渲染放大倍数")
    ap.add_argument("--wrap", type=int, default=16, help="渲染每行 tile 数")
    args = ap.parse_args()

    data = open(args.rom, "rb").read()

    if args.command == "list":
        m1 = need_magic(args.magic1, "magic1")
        for o in find_blobs(data, m1):
            d, used = container1_decode(data, o, True)
            print("0x%06X  src=%5d  out=%6d  tiles=%5.2f" % (o, used, len(d), len(d) / 32.0))
    elif args.command in ("decode", "render"):
        m1 = need_magic(args.magic1, "magic1")
        off = int(args.offset, 16)
        if data[off:off + 4] != m1:
            print("!! 0x%06X 处不是容器 1 魔数" % off)
            sys.exit(1)
        d, used = container1_decode(data, off, True)
        print("0x%06X: src=%d bytes -> out=%d bytes (%.2f tiles)" % (off, used, len(d), len(d) / 32.0))
        out = args.out or ("blob.bin" if args.command == "decode" else "blob.png")
        if args.command == "decode":
            open(out, "wb").write(d)
        else:
            im, _ = render_tiles(d, scale=args.scale, wrap=args.wrap)
            im.save(out)
        print("written:", out)
    elif args.command == "renderall":
        m1 = need_magic(args.magic1, "magic1")
        outdir = args.outdir or "out"
        os.makedirs(outdir, exist_ok=True)
        for o in find_blobs(data, m1):
            d, _ = container1_decode(data, o, True)
            im, nt = render_tiles(d, scale=args.scale, wrap=args.wrap)
            im.save(os.path.join(outdir, "blob_%06X.png" % o))
            print("0x%06X -> %d tiles" % (o, nt))
    elif args.command == "magic2":
        m2 = need_magic(args.magic2, "magic2")
        off = int(args.offset, 16)
        if data[off:off + 4] != m2:
            print("!! 0x%06X 处不是容器 2 魔数" % off)
            sys.exit(1)
        d, used = magic2_raw_decode(data, off)
        print("0x%06X: 原始拷贝 %d 字节" % (off, len(d)))
        out = args.out or "blob2.bin"
        open(out, "wb").write(d)
        print("written:", out)


if __name__ == "__main__":
    main()
