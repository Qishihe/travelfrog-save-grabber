import json, re

PRIME = 0x01000193
BASIS = 0x811C9DC5


def fnv1a_js(s):
    """与游戏内 JS 版 fnv1a 一致：按 UTF-16 code unit 逐字符。"""
    h = BASIS
    for ch in s:
        c = ord(ch)
        if c > 0xFFFF:
            c -= 0x10000
            units = (0xD800 + (c >> 10), 0xDC00 + (c & 0x3FF))
        else:
            units = (c,)
        for u in units:
            h ^= u
            h = (h * PRIME) & 0xFFFFFFFF
    return format(h, '08x')


def js_key_order(d):
    """JS 对象枚举顺序：整数键升序在前，其余保持插入顺序。"""
    idx, rest = [], []
    for k in d.keys():
        if isinstance(k, str) and re.fullmatch(r"0|[1-9][0-9]*", k):
            n = int(k)
            if 0 <= n <= 4294967294:
                idx.append((n, k))
                continue
        rest.append(k)
    idx.sort(key=lambda t: t[0])
    return [k for _, k in idx] + rest


def js_dumps(o):
    """按 JS JSON.stringify 的键序序列化（写回存档必须用这个）。"""
    if isinstance(o, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + js_dumps(o[k]) for k in js_key_order(o)) + "}"
    if isinstance(o, list):
        return "[" + ",".join(js_dumps(v) for v in o) + "]"
    if isinstance(o, bool):
        return "true" if o else "false"
    if o is None:
        return "null"
    if isinstance(o, str):
        return json.dumps(o, ensure_ascii=False)
    if isinstance(o, int):
        return str(o)
    if isinstance(o, float):
        return repr(o)
    raise TypeError(type(o))
