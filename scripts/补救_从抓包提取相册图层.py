# -*- utf-8 -*-
"""从已有抓包会话的 s2c.bin 里补提相册图层 / 美好瞬间（2026-11-26）。

背景：旧版注入器只认「回包带 cmd」，而服务端对注入请求的回包只有
{session, data} 没有 cmd，导致 album.load_by_id_list 的图层回包整段漏存，
照片显示空白。但原始 s2c.bin 里其实完整保存了这些帧。本脚本直接从
s2c.bin 提取，写回 album_by_id.jsonl / moment_by_inject.jsonl，
再重跑一次 grab_official 的合并与生成流程即可，无需重新抓包。

用法：
    python 补救_从抓包提取相册图层.py <抓包会话目录> <输出目录>

  <抓包会话目录>：tools/mitm/capture/<时间戳>_ali-lxqw-login.ejoy.com
  <输出目录>    ：output/一键抓官服_YYYYMMDD_HHMMSS  （里面有「协议」子目录）
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'tools', 'mitm'))
import ws_proxy as W  # noqa: E402


def take_frames(path):
    buf = bytearray(open(path, 'rb').read())
    while True:
        fr = W.take_frame(buf)
        if fr is None:
            break
        yield fr


def extract(cap_dir, out_dir):
    s2c = os.path.join(cap_dir, 's2c.bin')
    if not os.path.isfile(s2c):
        print('[X] 没有 s2c.bin：%s' % s2c)
        return 1
    pics, moments = [], []
    for op, payload in take_frames(s2c):
        if op != 1:
            continue
        try:
            o = json.loads(payload.decode('utf-8', 'ignore'))
        except Exception:
            continue
        ses = o.get('session')
        data = o.get('data')
        if not isinstance(data, dict):
            continue
        # 只认注入用的高 session（9000+），避免把 museum.load 的自然下发 pic_list 混进来
        if isinstance(ses, int) and ses >= 9000:
            pl = data.get('pic_list')
            if isinstance(pl, list) and pl:
                pics.extend(pl)
                print('   session %s -> %d 张图层' % (ses, len(pl)))
            lst = data.get('list')
            if isinstance(lst, list) and lst and all(isinstance(x, int) for x in lst):
                moments.append(lst)
                print('   session %s -> %d 个瞬间' % (ses, len(lst)))
    print('共提取：相册图层 %d 张，瞬间 %d 组' % (len(pics), len(moments)))

    # 去重（按 id 保留图层最全的一条）
    by_id = {}
    for p in pics:
        if not isinstance(p, dict) or p.get('id') is None:
            continue
        old = by_id.get(p['id'])
        if old is None or len(p.get('layers') or []) > len(old.get('layers') or []):
            by_id[p['id']] = p
    pics = list(by_id.values())
    print('去重后 %d 张（有图层 %d）' % (len(pics), sum(1 for x in pics if x.get('layers'))))

    # 写到抓包会话目录（grab_official 的 merge_injected_logs 会从这里读）
    if pics:
        with open(os.path.join(cap_dir, 'album_by_id.jsonl'), 'w', encoding='utf-8') as f:
            f.write(json.dumps({'cmd': 'album.load_by_id_list', 'pics': pics}, ensure_ascii=False) + '\n')
        print('已写 %s' % os.path.join(cap_dir, 'album_by_id.jsonl'))
    if moments:
        best = max(moments, key=len)
        with open(os.path.join(cap_dir, 'moment_by_inject.jsonl'), 'w', encoding='utf-8') as f:
            f.write(json.dumps({'cmd': 'misc.moment_load', 'data': {'list': best}}, ensure_ascii=False) + '\n')
        print('已写 %s' % os.path.join(cap_dir, 'moment_by_inject.jsonl'))

    # 直接合并进协议目录
    sys.path.insert(0, HERE)
    import grab_official as G
    added = G.merge_injected_logs([cap_dir], os.path.join(out_dir, '协议'))
    print('已并入协议目录：%s' % ('、'.join(added) if added else '无'))
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print(__doc__)
        raise SystemExit(1)
    raise SystemExit(extract(sys.argv[1], sys.argv[2]))
