# -*- utf-8 -*-
"""解析中间人抓到的 WebSocket 明文，拆成协议 JSON。

用法：
    python parse_ws.py capture/<会话目录> [输出目录]
产物：
    <输出>/<cmd>.json     每个协议一个文件
    <输出>/_messages.jsonl  全部消息（按到达顺序，含非 JSON）
"""
import json
import os
import sys


def frames(fp):
    """逐个产出 (fin, opcode, payload)。"""
    while True:
        h = fp.read(2)
        if len(h) < 2:
            return
        b0, b1 = h[0], h[1]
        fin = bool(b0 & 0x80)
        op = b0 & 0x0F
        masked = b1 & 0x80
        ln = b1 & 0x7F
        if ln == 126:
            ln = int.from_bytes(fp.read(2), 'big')
        elif ln == 127:
            ln = int.from_bytes(fp.read(8), 'big')
        mask = fp.read(4) if masked else None
        data = fp.read(ln)
        if len(data) < ln:
            return
        if mask:
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        yield fin, op, data


def messages(path):
    with open(path, 'rb') as fp:
        buf = bytearray()
        for fin, op, data in frames(fp):
            if op in (0x8, 0x9, 0xA):   # close/ping/pong 不参与消息体
                continue
            buf += data
            if fin:
                yield buf.decode('utf-8', 'ignore')
                buf.clear()


def main():
    src = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(src, 'protocols')
    os.makedirs(out, exist_ok=True)
    s2c = os.path.join(src, 's2c.bin')
    if not os.path.exists(s2c):
        print('没有 s2c.bin')
        return 1
    n = 0
    fout = open(os.path.join(out, '_messages.jsonl'), 'w', encoding='utf-8')
    for msg in messages(s2c):
        n += 1
        fout.write(msg.replace('\n', ' ') + '\n')
        try:
            o = json.loads(msg)
        except Exception:
            continue
        cmd = o.get('cmd') or o.get('name') or 'unknown_%d' % n
        data = o.get('data', o.get('params'))
        size = len(json.dumps(o, ensure_ascii=False))
        with open(os.path.join(out, cmd + '.json'), 'w', encoding='utf-8') as f:
            json.dump(o, f, ensure_ascii=False, indent=1)
        dk = list(data.keys())[:6] if isinstance(data, dict) else ('<list:%d>' % len(data) if isinstance(data, list) else type(data).__name__)
        print('  %-32s %7dB  %s' % (cmd, size, dk))
    fout.close()
    print('共 %d 条消息，输出目录 %s' % (n, out))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
