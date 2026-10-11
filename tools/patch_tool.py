# -*- coding: utf-8 -*-
"""
patch_tool.py — 移除画面中译文组署名水印与模式指示器
=====================================================
原理（详见 docs/04-watermark-and-patch.md）:
  这些元素不是代码绘制的文字，而是自定义容器格式的位图资源（8x8 tile 拼成）。
  它们由画面装载表通过 `jsr <set_vram_target>` + `jsr <decode>` 上传到 VRAM，
  再由场景 tilemap 或固定 UI 引用。

  因此移除方式不需要动地图数据或绘制代码：
  把资源本身改写成「解码结果全为零」的最小流，使贴图变成全透明 tile。
  所有引用它的画面会一次性生效。

补丁对象（共 13 个资源）:
  署名水印    2 个（同一图形的两个调色板变体，必须都改，否则部分场景仍有残留）
  模式指示器 11 个（MODE 标签中/英各一 + 配套元素 + 四个难度档位名 + 英文档位名组）

  这 13 个资源的地址在全 ROM 中各只被自身的装载表引用一次，无其他画面共用。

用法:
  python patch_tool.py --rom base_unp.bin --magic1 <HEX8> [--out patched.bin]
  python patch_tool.py --rom base_unp.bin --magic1 <HEX8> --no-mode-blobs   # 只去水印

脱敏说明:
  容器魔数已从本仓库移除，需通过 --magic1 传入你自己的值。
  偏移量为结构信息，保持原值以便与文档对照。
"""
import argparse
import os
import sys

# ---- 占位符：请填入目标 ROM 的容器魔数（8 位十六进制 = 4 字节 ASCII）----
DEFAULT_MAGIC1 = "00000000"

# ---- 署名水印（两个调色板变体，形状相同，必须同时处理）----
WATERMARK_BLOBS = [
    (0x0DFE54, 384, "水印 变体A"),
    (0x0DFD02, 384, "水印 变体B"),
]

# ---- 模式指示器（战斗画面常驻）----
# 装载器 0xDC65E，资源表 0xDC6E2：按 RAM $FF5FA3 的模式值 0-3 取指针；
# RAM $FF5FA0 == 2 时改用英文组（表项 +0x10）。
MODE_BLOBS = [
    (0x374C3E, 192, "MODE标签(中)"),
    (0x374CF4, 192, "MODE标签(英)"),
    (0x3749E0, 256, "配套元素"),
    (0x0F7DB0, 192, "档位名0"),
    (0x0F7E64, 192, "档位名1"),
    (0x0F7F12, 192, "档位名2"),
    (0x374D7A, 192, "档位名3"),
    (0x374E26, 192, "EASY"),
    (0x374EAA, 192, "NORMAL"),
    (0x374F6E, 192, "HARD"),
    (0x374FF4, 192, "HIDDEN"),
]

MODE_LOADER_ADDR = 0xDC65E
MODE_TABLE_ADDR = 0xDC6E2
MODE_VAR_RAM = 0xFF5FA3


def parse_magic(text):
    s = text.strip().lower().replace("0x", "").replace(" ", "")
    if len(s) != 8 or any(c not in "0123456789abcdef" for c in s):
        raise argparse.ArgumentTypeError("魔数需为 8 位十六进制字符（4 字节）")
    if s == "00000000":
        raise argparse.ArgumentTypeError(
            "魔数还是占位值。本仓库已脱敏，请通过 --magic1 传入你自己的值")
    return bytes.fromhex(s)


def nib_remap(x):
    m = {7: 10, 10: 12, 12: 7}
    return (m.get(x >> 4, x >> 4) << 4) | m.get(x & 0xF, x & 0xF)


def container_decode(data, off):
    """解码容器 1；off 指向魔数首字节"""
    i = off + 4
    out = bytearray()
    n = len(data)
    guard = 0
    while True:
        guard += 1
        if guard > 8_000_000 or i >= n:
            break
        c = data[i]
        i += 1
        if c == 0:
            break
        if c < 0x80:
            for _ in range(c):
                if i >= n:
                    return bytes(out)
                v = data[i]
                i += 1
                out.append(nib_remap(v))
        else:
            k = (c & 0x7F) + 1
            v = out[-1] if out else 0
            out += bytes([v]) * k
    return bytes(out)


def zero_stream(n):
    """构造解码出 n 个 0x00 的最小容器流。

    结构: 1 个字面量 0x00 + 若干 RLE 段 + 终止符 0x00
    RLE 控制字节 c (>=0x80) 表示重复上一个字节 (c & 0x7F) + 1 次。
    """
    assert n >= 1, "长度必须 >= 1"
    out = [0x01, 0x00]
    n -= 1
    while n > 0:
        k = min(n, 128)
        out.append(0x80 + k - 1)
        n -= k
    out.append(0x00)
    return bytes(out)


def main():
    ap = argparse.ArgumentParser(description="移除署名水印与模式指示器")
    ap.add_argument("--rom", required=True, help="输入 ROM（建议使用已移除保护机制的基底）")
    ap.add_argument("--out", help="输出 ROM 路径，默认在原文件旁生成 *._patched.bin")
    ap.add_argument("--magic1", type=parse_magic, default=DEFAULT_MAGIC1,
                    help="容器魔数（8 位十六进制）")
    ap.add_argument("--no-mode-blobs", action="store_true",
                    help="只处理署名水印，保留模式指示器")
    args = ap.parse_args()

    src = args.rom
    dst = args.out or os.path.splitext(src)[0] + "_patched.bin"
    raw = open(src, "rb").read()
    data = bytearray(raw)
    print("输入: %s  (%d 字节)" % (src, len(data)))
    if data[0x100:0x104] != b"SEGA":
        print("!! 头部 0x100 处没有 SEGA 魔数，可能不是目标平台的 ROM")
        sys.exit(1)
    if len(data) != 0x400000:
        print("!! 警告: ROM 大小不是 4 MB (%d 字节)，偏移量可能不适用" % len(data))

    targets = list(WATERMARK_BLOBS)
    if not args.no_mode_blobs:
        targets += list(MODE_BLOBS)

    for off, ln, name in targets:
        if data[off:off + 4] != args.magic1:
            print("!! 0x%06X 处不是容器魔数 (%s)" % (off, name))
            sys.exit(1)
        dec = container_decode(bytes(data), off)
        if len(dec) != ln:
            print("!! 0x%06X %s 解码长度 %d != 预期 %d" % (off, name, len(dec), ln))
            sys.exit(1)
        print("  [检查] 0x%06X %-12s 解码 %d 字节 OK" % (off, name, len(dec)))

    for off, ln, name in targets:
        zs = zero_stream(ln)
        s = off + 4
        old = bytes(data[s:s + len(zs)])
        data[s:s + len(zs)] = zs
        print("  [补丁] 0x%06X %-12s 流 %s -> %s" % (off, name, old.hex(), zs.hex()))

    ok = True
    for off, ln, name in targets:
        dec = container_decode(bytes(data), off)
        good = (len(dec) == ln and set(dec) == {0})
        ok &= good
        print("  [验证] 0x%06X %-12s 解码 %d 字节 全零=%s -> %s"
              % (off, name, len(dec), set(dec) == {0}, "透明化成功" if good else "失败!"))
    if not ok:
        print("!! 验证失败，不写出文件")
        sys.exit(1)

    changed = sum(1 for a, b in zip(data, raw) if a != b)
    open(dst, "wb").write(bytes(data))
    print("\n输出: %s" % dst)
    print("改动字节数: %d（仅在资源流内部，其余未动）" % changed)
    if args.no_mode_blobs:
        print("提示: 已保留模式指示器（未处理 %d 个资源）" % len(MODE_BLOBS))


if __name__ == "__main__":
    main()
