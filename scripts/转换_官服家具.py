# -*- coding: utf-8 -*-
"""官服家具抓包 -> 单机版存档 furniture 模块（含 extras 堆肥/口袋/花盆）。

原作者没有随包交付这一步：lib/抓取包 能抓到 furniture.load_furniture 等协议，
但包里没有脚本把它转成离线存档的 furniture 字典。本脚本按 v37 的实际产物逆推
字段映射与类型规则，并给出两种验证口径（务必分清，否则会误判"对拍失败"）：

  * 结构/类型对拍（默认 --check）：协议是"今天这个账号"的真实进度，v37 是作者
    "官服全量内容"底稿，两者是不同账号，值必然不同。能对拍的是字段名、类型、
    取值域、顺序规则——本脚本对 v37 的结构一致性做严格校验。
  * 全量口径对拍（--check-full）：验证 has_fur 的全量重建规则
    = furnitureData 配置表 405 项 - 6 个礼盒类(10101..10106) = 399，与 v37 集合完全一致。

抓包 -> 存档 字段映射：
  furniture.load_furniture:
    has_fur      has_fur      原样（官服 id 列表）
    put_fur      put_fur      原样 [{id,type}...]
    bench        bench        原样（10 槽，含 -1 空位、可重复）
    replace_fur  replace_fur  原样
    mood         mood         原样 int（协议是 bool/int 都转 int）
    bench_lock   bench_lock   bool -> int（v37 存 0/1）
    mate_list    mate_list    协议 {} -> 离线 []
    shop.shop_list {item_id,shop_id,num} -> {shop_id,item_id,num,free,seasonal,daily?}
                 free/seasonal 由 furnitureShopData 的 sign/type 推导（详见 shop_item）
    shop.start_time/leave_time 原样
  furniture.load_compost   -> furniture.extras.compost
  furniture.load_pocket    -> furniture.extras.pocket   (clover 原样)
  furniture.load_flowerpot -> furniture.extras.flowerpot

离线独有、协议不提供的字段（rotation/dailyShop/theme/weeklyShop/tumbler/
courtyardMatDiag/basicStyleGranted 等）一律从 base 存档保留，不被协议覆盖——
这些是单机版自己的玩法状态，官服没有对应下发。

用法：
  # 1) 结构对拍（协议 + v37 参考档）
  python 转换_官服家具.py --proto lib/抓取包/输出/protocols \
      --base output/旅行青蛙存档_官服相册_v37.json --check

  # 2) 全量口径对拍（验证 has_fur 399 重建规则）
  python 转换_官服家具.py --base output/旅行青蛙存档_官服相册_v37.json --check-full

  # 3) 真用：把你抓到的家具协议写进自己的存档
  python 转换_官服家具.py --proto protocols --base 我的存档.json --out 输出.json
  # 想用"全家具"底稿（而非自己账号真实持有量）：加 --full-has-fur
"""
import argparse
import json
import os
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PKG, 'lib'))
sys.stdout.reconfigure(encoding='utf-8')
import tf                                   # noqa: E402

CFG = os.path.join(PKG, 'data', 'config_json')
FUR_DATA = os.path.join(CFG, 'furnitureData_json.json')
SHOP_DATA = os.path.join(CFG, 'furnitureShopData_json.json')

# 官服 has_fur 全量口径（v37 逆推）：furnitureData 405 项，排除 10101..10106 六个礼盒/特殊类
GIFT_IDS = {10101, 10102, 10103, 10104, 10105, 10106}

# 离线版对特定 shop_id 的硬编码玩法标记（官服协议不下发，v37 逆推）
SHOP_SPECIAL = {
    6010: {'daily': 'seed'},
    7027: {'daily': 'gift', 'free': True},
    1: {'toolBag': True, 'free': True},
}

# 离线 furniture 里协议可直接提供的键（其余键是离线独有，必须从 base 保留）
PROTO_KEYS = ('has_fur', 'put_fur', 'bench', 'replace_fur', 'mood',
              'bench_lock', 'mate_list', 'shop')


def load_any(path):
    with open(path, encoding='utf-8') as f:
        obj = json.load(f)
    if isinstance(obj, dict) and 'data' in obj and 'salt' in obj:
        return tf.load(path)
    return obj, None


def read_protocols(proto):
    """proto 可以是目录（按 <cmd>.json）或单个 JSON 文件。返回 cmd->data。"""
    out = {}
    if os.path.isdir(proto):
        for fn in sorted(os.listdir(proto)):
            if not fn.lower().endswith('.json'):
                continue
            try:
                o = json.load(open(os.path.join(proto, fn), encoding='utf-8'))
            except Exception:
                continue
            if isinstance(o, dict) and 'cmd' in o and 'data' in o:
                out[o['cmd']] = o['data']
    else:
        o = json.load(open(proto, encoding='utf-8'))
        if isinstance(o, dict) and 'cmd' in o and 'data' in o:
            out[o['cmd']] = o['data']
        elif isinstance(o, list):
            for x in o:
                if isinstance(x, dict) and 'cmd' in x:
                    out[x['cmd']] = x.get('data', {})
    return out


def full_has_fur(fur_data):
    ids = {int(k) for k in fur_data}
    return sorted(ids - GIFT_IDS)


def shop_item(item, shop_cfg):
    """协议 shop_list 项 {item_id,shop_id,num} -> 离线项（v37 字段顺序）。"""
    sid = item.get('shop_id')
    cfg = shop_cfg.get(str(sid)) or shop_cfg.get(sid) or {}
    special = SHOP_SPECIAL.get(sid, {})
    free = bool(item.get('free', special.get('free', cfg.get('sign') == 3)))
    seasonal = bool(item.get('seasonal', False))
    e = {'shop_id': sid, 'item_id': item.get('item_id'), 'num': item.get('num', 0),
         'free': free, 'seasonal': seasonal}
    # daily / toolBag 是离线玩法标记，协议不下发；按 shop_id 查硬编码表
    for k in ('daily', 'toolBag'):
        if k in item:
            e[k] = item[k]
        elif k in special:
            e[k] = special[k]
    return e


def build_furniture(protos, base_fur, fur_data, shop_cfg, full_has=False):
    """把协议数据并入 base 的 furniture。返回 (新 furniture, 统计)。"""
    fur = dict(base_fur or {})
    stat = Counter()

    d = protos.get('furniture.load_furniture')
    if d:
        for k in PROTO_KEYS:
            if k not in d:
                continue
            if k == 'shop':
                sp = d['shop'] or {}
                e = {'start_time': sp.get('start_time'),
                     'leave_time': sp.get('leave_time'),
                     'shop_list': [shop_item(x, shop_cfg) for x in (sp.get('shop_list') or [])]}
                fur['shop'] = e
                stat['shop'] = len(e['shop_list'])
            elif k == 'bench_lock':
                fur[k] = 1 if d[k] else 0
                stat[k] = 1
            elif k == 'mate_list':
                fur[k] = list(d[k]) if isinstance(d[k], dict) else (d[k] or [])
                stat[k] = len(fur[k])
            elif k == 'mood':
                fur[k] = int(d[k]) if not isinstance(d[k], bool) else int(d[k])
                stat[k] = 1
            else:
                fur[k] = d[k]
                stat[k] = len(d[k]) if isinstance(d[k], (list, dict)) else 1

    if full_has:
        fur['has_fur'] = full_has_fur(fur_data)
        stat['has_fur(全量)'] = len(fur['has_fur'])

    # extras：三个子协议原样映射（字段名已与离线一致），tumbler 等离线独有字段保留
    extras = dict((base_fur or {}).get('extras') or {})
    mapping = {
        'furniture.load_compost': ('compost', ('compost_list', 'show_index', 'replace_index',
                                                'box_index', 'state', 'box_list')),
        'furniture.load_pocket': ('pocket', ('list', 'show_index', 'replace_index', 'clover')),
        'furniture.load_flowerpot': ('flowerpot', ('list', 'show_index', 'replace_index', 'plant_list')),
    }
    for cmd, (key, fields) in mapping.items():
        if cmd in protos:
            src = protos[cmd]
            e = dict(extras.get(key) or {})
            for f in fields:
                if f in src:
                    e[f] = src[f]
            extras[key] = e
            stat['extras.' + key] = 1
    fur['extras'] = extras
    return fur, stat


def check_structure(built, ref):
    """结构/类型对拍：字段名集合、类型、取值域（不同账号不比具体值）。"""
    print('=== 结构对拍（协议 vs v37 参考档）===')
    bf, rf = built.get('furniture', built), ref.get('furniture', ref)
    ok = True
    # 1) 协议可提供的键必须都存在且类型一致
    for k in PROTO_KEYS:
        bv, rv = bf.get(k), rf.get(k)
        if bv is None or rv is None:
            print('  [X] %-12s 缺失 built=%s ref=%s' % (k, bv is not None, rv is not None))
            ok = False
            continue
        tb, tr = type(bv).__name__, type(rv).__name__
        same = tb == tr
        print('  %s %-12s type=%-5s ref=%-5s len=%s' %
              ('OK' if same else 'X', k, tb, tr, len(bv) if isinstance(bv, (list, dict)) else '-'))
        ok &= same
    # 2) 离线独有字段必须原样保留（不能被协议冲掉）
    offline_only = [k for k in rf if k not in PROTO_KEYS and k != 'extras']
    missed = [k for k in offline_only if k not in bf]
    print('  离线独有字段 %d 个，保留 %d 个' % (len(offline_only), len(offline_only) - len(missed)))
    if missed:
        print('  [X] 丢失:', missed)
        ok = False
    # 3) shop 项结构：不同账号商店快照不同，只校验每项核心字段合法 + 特殊商品标记
    bs = (bf.get('shop') or {}).get('shop_list') or []
    rs = (rf.get('shop') or {}).get('shop_list') or []
    core = {'shop_id', 'item_id', 'num', 'free', 'seasonal'}
    bad_core = [x for x in bs if not core.issubset(x)]
    print('  shop_list 项数: built=%d ref=%d | 核心字段齐全: %s'
          % (len(bs), len(rs), '是' if not bad_core else '否 %s' % bad_core[:2]))
    if bad_core:
        ok = False
    # 特殊商品标记（出现在协议里时才应有）
    special_bad = []
    for x in bs:
        sp = SHOP_SPECIAL.get(x.get('shop_id'), {})
        for k, v in sp.items():
            if x.get(k) != v:
                special_bad.append((x.get('shop_id'), k, x.get(k), v))
    print('  特殊商品标记(daily/gift/toolBag):', '一致' if not special_bad else special_bad)
    if special_bad:
        ok = False
    # 4) bool->int 类型
    print('  bench_lock: built=%r(%s) ref=%r(%s)' %
          (bf.get('bench_lock'), type(bf.get('bench_lock')).__name__,
           rf.get('bench_lock'), type(rf.get('bench_lock')).__name__))
    # 5) extras
    for k in ('compost', 'pocket', 'flowerpot', 'tumbler'):
        b = (bf.get('extras') or {}).get(k)
        r = (rf.get('extras') or {}).get(k)
        print('  extras.%-10s built=%s ref=%s' % (k, '有' if b else '无', '有' if r else '无'))
        if r and not b:
            ok = False
    print('\n结论:', '结构一致（值不同属正常：不同账号）' if ok else '有结构差异，见 [X]')
    return 0 if ok else 1


def check_full(ref, fur_data):
    """全量口径对拍：has_fur 399 重建规则。"""
    full = set(full_has_fur(fur_data))
    rf = (ref.get('furniture') or {}).get('has_fur') or []
    got = set(rf)
    print('=== 全量口径对拍（has_fur）===')
    print('  配置表重建: %d 个（405 - %d 礼盒）' % (len(full), len(GIFT_IDS)))
    print('  v37 参考档: %d 个' % len(got))
    inter = len(full & got)
    print('  交集: %d | 重建多出: %d | 参考多出: %d' %
          (inter, len(full - got), len(got - full)))
    ok = full == got
    print('  集合完全一致:', '是 ✅' if ok else '否 ❌')
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--proto', help='抓包 protocols 目录（含 furniture.load_*.json）')
    ap.add_argument('--base', help='单机版存档（加密/明文都行）')
    ap.add_argument('--out', help='输出存档路径')
    ap.add_argument('--check', action='store_true', help='结构对拍（需 --proto 和 --base）')
    ap.add_argument('--check-full', action='store_true', help='全量 has_fur 对拍（需 --base）')
    ap.add_argument('--full-has-fur', action='store_true', help='真用时写入 399 全量家具（而非账号真实持有）')
    ap.add_argument('--fur-data', default=FUR_DATA)
    ap.add_argument('--shop-data', default=SHOP_DATA)
    a = ap.parse_args()

    if not a.base:
        print('[X] 需要 --base（单机版存档）')
        return 1
    base, env = load_any(a.base)
    fur_data = json.load(open(a.fur_data, encoding='utf-8'))
    shop_cfg = json.load(open(a.shop_data, encoding='utf-8'))

    if a.check_full:
        return check_full(base, fur_data)

    if a.check:
        if not a.proto:
            print('[X] --check 需要 --proto')
            return 1
        protos = read_protocols(a.proto)
        built, stat = build_furniture(protos, base.get('furniture'), fur_data, shop_cfg)
        print('协议命中:', ', '.join(sorted(protos)) or '(无)')
        print('映射统计:', dict(stat))
        print()
        return check_structure({'furniture': built}, {'furniture': base.get('furniture') or {}})

    if not a.proto or not a.out:
        print(__doc__)
        return 1

    protos = read_protocols(a.proto)
    if not protos:
        print('[X] 没有读到任何协议:', a.proto)
        return 1
    built, stat = build_furniture(protos, base.get('furniture'), fur_data, shop_cfg,
                                  full_has=a.full_has_fur)
    print('协议命中:', ', '.join(sorted(protos)))
    print('映射统计:', dict(stat))
    base['furniture'] = built

    now = int(time.time())
    base_ms = int(base.get('lastSaveTimeMs') or 0)
    base['lastSaveTime'] = now
    base['lastSaveTimeMs'] = max(base_ms, now * 1000) + 3600 * 1000

    if env:
        env['exportedAt'] = now * 1000
        env['exportedAtText'] = time.strftime('%Y-%m-%d %H:%M', time.localtime(now))
        tf.save(a.out, base, env['salt'], env)
        chk, _ = tf.load(a.out)
        cf = chk.get('furniture') or {}
        print('写入: %s（加密存档）' % a.out)
        print('回读校验: has_fur=%d put_fur=%d shop=%d extras=%s'
              % (len(cf.get('has_fur') or []), len(cf.get('put_fur') or []),
                 len((cf.get('shop') or {}).get('shop_list') or []),
                 sorted((cf.get('extras') or {}).keys())))
    else:
        json.dump(base, open(a.out, 'w', encoding='utf-8'), ensure_ascii=False)
        print('写入: %s（明文）' % a.out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
