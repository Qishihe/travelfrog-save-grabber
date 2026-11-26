"""极简 PNG 解码 + 亮度统计（纯标准库，不依赖 PIL）。

为什么需要它：绿色包用的是裁剪过的内嵌 Python，没装 PIL，
但「启动页 / 博物馆页」这类自绘界面只能靠截图亮度来判断，
所以这里自己解一下 PNG——只支持 Android screencap 产出的格式
（8bit、灰度 0 / RGB 2 / RGBA 6、非隔行），够用且不会拖慢启动。

用法：
    import pnglite
    ratio = pnglite.hi_ratio(png_bytes)      # 亮度 > 200 的像素占比；失败返回 None
"""

import struct
import zlib

SIG = b'\x89PNG\r\n\x1a\n'


def _unfilter(raw, w, h, bpp):
    """按 PNG 规范逐行反过滤（None/Sub/Up/Average/Paeth）。返回拼好的像素字节。"""
    stride = w * bpp
    out = bytearray(stride * h)
    pos = 0
    prev = bytearray(stride)
    for y in range(h):
        ft = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if ft == 1:                      # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:                    # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:                    # Average
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:                    # Paeth
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa = abs(p - a)
                pb = abs(p - b)
                pc = abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        elif ft != 0:
            raise ValueError('bad filter %d' % ft)
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return bytes(out)


def decode(raw):
    """解码 PNG，返回 (width, height, channels, pixels)。失败抛 ValueError。"""
    if not raw or raw[:8] != SIG:
        raise ValueError('not a png')
    pos = 8
    w = h = depth = ctype = None
    idat = []
    while pos + 8 <= len(raw):
        ln = struct.unpack('>I', raw[pos:pos + 4])[0]
        typ = raw[pos + 4:pos + 8]
        data = raw[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b'IHDR':
            if ln < 10:
                raise ValueError('short ihdr')
            w, h, depth, ctype = struct.unpack('>IIBB', data[:10])
        elif typ == b'IDAT':
            idat.append(data)
        elif typ == b'IEND':
            break
    if not w or not h or depth != 8 or ctype not in (0, 2, 6):
        raise ValueError('unsupported png (w=%s h=%s depth=%s type=%s)' % (w, h, depth, ctype))
    bpp = {0: 1, 2: 3, 6: 4}[ctype]
    try:
        raw_px = zlib.decompress(b''.join(idat))
    except Exception as e:
        raise ValueError('zlib: %s' % e)
    need = (w * bpp + 1) * h
    if len(raw_px) < need:
        raise ValueError('truncated pixels')
    return w, h, bpp, _unfilter(raw_px, w, h, bpp)


def page_stats(raw, sample=6):
    """一次解码给出整屏特征，解码失败返回 None。

    返回 dict（值都是 0~1 的占比）：
      brightness   亮度 >200 的占比   启动页 0.88 / 博物馆页 0.66 / 小仓库弹窗 0.39 / 庭院 0.11
      beige        「米黄纸张」占比   博物馆页 0.56 / 各类弹窗 0.34~0.36 / 启动页 0.01 / 庭院 0
      edge_beige   左右两侧 6% 边缘里的米黄占比
                   博物馆页是全屏纸张（0.64），弹窗型和场景全是 0 —— 这条最能区分「真页面」和「弹窗」
      dark         亮度 <90 的占比
    """
    try:
        w, h, bpp, px = decode(raw)
    except Exception:
        return None
    xl = int(w * 0.06)
    xr = w - xl
    tot = bright = dark = beige = etot = ebeige = 0
    for y in range(0, h, sample):
        base = y * w * bpp
        for x in range(0, w, sample):
            o = base + x * bpp
            r, g, b = px[o], px[o + 1], px[o + 2]
            lum = 0.299 * r + 0.587 * g + 0.114 * b
            tot += 1
            if lum > 200:
                bright += 1
            elif lum < 90:
                dark += 1
            is_beige = (r > 195 and g > 180 and b < 215 and (r - b) >= 25)
            if is_beige:
                beige += 1
            if x < xl or x >= xr:
                etot += 1
                if is_beige:
                    ebeige += 1
    if not tot:
        return None
    return {
        'brightness': bright / float(tot),
        'beige': beige / float(tot),
        'edge_beige': (ebeige / float(etot)) if etot else 0.0,
        'dark': dark / float(tot),
    }


def hi_ratio(raw, threshold=200, sample=4):
    """整屏里亮度 > threshold 的像素占比（0~1）。解码失败返回 None。

    保留给「启动页」这种只需要亮度的场景；要看页面类型用 page_stats。
    """
    st = page_stats(raw, sample=sample)
    return None if st is None else st['brightness']
