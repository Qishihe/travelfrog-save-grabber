# -*- coding: utf-8 -*-
"""把抓到的米服协议，按 v37 底稿的格式，编排成一份可直接导入的存档 JSON。

用法：
  python scripts\\生成实号存档.py --proto lib\\抓取包\\输出\\protocols_ws_20261124 ^
      --base output\\模板文件.json --out output\\旅行青蛙存档_实号_20261124.json

  --real-fur     家具 has_fur 用账号真实持有（172）；默认用 v37 全量 399（能摆满）
  --keep-tumbler 不倒翁保留 v37 全量 156；默认换成账号真实的 12
  --with-album   相册也换成真实 245 张（注意：线上协议没有 layers，伙伴/图层会丢）
  --keep-achieves 称号保留 v37 的 107 个（默认用账号真实 38 个）
  --plain        输出明文 JSON（不加密，仅供查看，不能直接导入）

每个模块都会先做结构校验：字段集不一致就跳过并报出来，不会把坏数据写进存档。
"""
import argparse
import importlib.util
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'lib'))
import tf  # noqa: E402


def _load_mod(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


FUR = _load_mod('conv_fur', os.path.join(HERE, '转换_官服家具.py'))


def read_protos(d):
    out = {}
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.endswith('.json') or fn.startswith('_'):
            continue
        p = os.path.join(d, fn)
        try:
            o = json.load(open(p, encoding='utf-8'))
        except Exception:
            continue
        if isinstance(o, dict) and o.get('cmd'):
            out[o['cmd']] = o
        elif isinstance(o, dict):
            # 未识别协议 cmd 为空：用文件名做键，避免多个 None 互相覆盖
            # （博物馆 museum_load 回包就常是 unknown_NN）
            out[fn[:-5]] = o
    return out


def data_of(protos, cmd):
    o = protos.get(cmd)
    return (o or {}).get('data')


def same_keys(a, b):
    return set(a) == set(b)


# ---------------------------------------------------------------- 各模块映射

def map_role(save, protos, keep_achieves=False):
    d = data_of(protos, 'client.load_role')
    if not d or 'frog' not in d:
        return '跳过：没有 client.load_role'
    f, r, res = d['frog'], save['role'], d.get('res') or {}
    before = (r.get('name'), r.get('clover'), r.get('ticket'), len(r.get('achieves') or []))
    if f.get('name'):
        r['name'] = f['name']
        r['isNamed'] = True
    for k in ('cur_achieve', 'status', 'motion', 'icon', 'today_step'):
        if k in f:
            r[k] = f[k]
    # achieves_time 必须落成**数组**：官服下发的是"限时称号到期表"，没有限时称号时就是 {}，
    # 而单机版 Progress.canEquip 里直接 (achieves_time || []).some(...) —— 传对象会抛
    # TypeError: .some is not a function，save() 抛异常后被 applyImport 吞掉，导入最终报 E7。
    at = f.get('achieves_time')
    if keep_achieves and isinstance(r.get('achieves_time'), list):
        pass                                  # 保留底稿那 103 项到期表
    elif isinstance(at, list):
        r['achieves_time'] = [x for x in at if isinstance(x, dict) and 'id' in x]
    elif isinstance(at, dict) and at:
        r['achieves_time'] = [{'id': int(k), 'time': v} for k, v in at.items()]
    else:
        r['achieves_time'] = []          # 空表 {} / 缺失 -> 空数组
    if not keep_achieves and 'achieves' in f:
        r['achieves'] = f['achieves']
    # 兜底：底稿或协议给的仍是异类（None/对象）时，一律归成数组，别把 E7 带进存档
    if not isinstance(r.get('achieves_time'), list):
        r['achieves_time'] = []
    if 'clover_point' in res:
        r['clover'] = res['clover_point']
    if 'ticket' in res:
        r['ticket'] = res['ticket']
    after = (r.get('name'), r.get('clover'), r.get('ticket'), len(r.get('achieves') or []))
    return '昵称/三叶草/抽奖券/称号 %s -> %s' % (before, after)


def map_items(save, protos):
    d = data_of(protos, 'item.load_items')
    if not d:
        return '跳过：没有 item.load_items'
    msg = []
    if d.get('house'):
        ref = save.get('house') or []
        rk = set().union(*(set(x) for x in ref if isinstance(x, dict))) if ref else set()
        pk = set().union(*(set(x) for x in d['house'] if isinstance(x, dict)))
        if rk and not pk.issubset(rk):
            msg.append('house 字段异常 %s，沿用底稿' % sorted(pk - rk))
        else:
            save['house'] = [{'item_id': x.get('item_id'), 'count': x.get('count')} for x in d['house']]
            msg.append('仓库 %d -> %d' % (len(ref), len(save['house'])))
    if isinstance(d.get('desk'), list):
        msg.append('桌面 %d' % len(d['desk']))
        save['desk'] = d['desk']
    if isinstance(d.get('bag'), list):
        save['bag'] = d['bag']
    if 'bag_completed' in d:
        save['bag_completed'] = d['bag_completed']
    return '；'.join(msg) or '无可用字段'


def map_notes(save, protos):
    d = data_of(protos, 'travel.load_note')
    lst = (d or {}).get('note_list')
    if not lst:
        return '跳过：没有 travel.load_note'
    ref = save.get('notes') or []
    pk = set().union(*(set(x) for x in lst if isinstance(x, dict)))
    rk = set().union(*(set(x) for x in ref if isinstance(x, dict))) if ref else set()
    if rk and not pk.issubset(rk):
        return '跳过：字段异常 %s' % sorted(pk - rk)
    save['notes'] = [{'id': x.get('id'), 'read': bool(x.get('read')), 'timestamp': x.get('timestamp')}
                     for x in lst]
    return '见闻 %d -> %d' % (len(ref), len(save['notes']))


def map_handcraft(save, protos):
    d = data_of(protos, 'pray.load_grays')
    if not d:
        return '跳过：没有 pray.load_grays'
    h = save.get('handcraft') or {}
    msg = []
    if h.get('stamps') is not None and isinstance(d.get('stamps'), list):
        ref = h['stamps']
        cnt = {}
        for x in ref:
            if isinstance(x, dict) and x.get('id') is not None:
                cnt.setdefault((x.get('u_id'), x.get('id')), x.get('count', 1))
        h['stamps'] = [{'u_id': x.get('u_id'), 'id': x.get('id'), 'state': x.get('state'),
                        'count': cnt.get((x.get('u_id'), x.get('id')), 1), 'time': x.get('time')}
                       for x in d['stamps']]
        msg.append('印章 %d -> %d' % (len(ref), len(h['stamps'])))
    if isinstance(d.get('wishs'), list) and h.get('wishs') is not None:
        ref = h.get('wishs') or []
        h['wishs'] = d['wishs']
        msg.append('祈愿 %d -> %d' % (len(ref), len(h['wishs'])))
    save['handcraft'] = h
    return '；'.join(msg) or '无可用字段'


# 厨房配方料理：long_id -> 料理 item_id（2026-11-26）。
# 单机版 offline-encytravel.js 的 payload() 里，凡「已解锁 + 属于配方料理 + kitchen.cooked[item]!=true」
# 的条目，都会在食物格右上角画问号。官服没有厨房玩法，抓来的档 cooked 必为空 → 解锁的配方料理全是问号。
# 这份表由 encytravel_json.json 的 list（long_id→item_id）与 offline-kitchen.js 的四张配方表
# （FESTIVAL/RARE/ORDINARY/REGIONAL 共 76 道）交叉生成，两套映射已核对一致；内置免依赖外部文件。
RECIPE_LONG_TO_ITEM = {
    10070001: 15, 10080001: 16, 10100001: 33, 10110001: 17, 10120001: 18, 10130001: 32, 10140001: 34, 10150001: 54,
    10160001: 55, 10170001: 56, 10180001: 57, 11010001: 21, 11020001: 36, 11030001: 22, 11040001: 37, 11050001: 38,
    11060001: 23, 11070001: 39, 11080001: 24, 11090001: 40, 11100001: 25, 11110001: 41, 11120001: 26, 11130001: 27,
    11140001: 42, 11150001: 43, 11160001: 28, 11170001: 44, 11180001: 29, 11190001: 30, 11200001: 45, 11210001: 31,
    11220001: 46, 11230001: 35, 11240001: 20, 12010001: 101, 12020001: 102, 12030001: 103, 12040001: 104, 12050001: 105,
    12060001: 106, 12070001: 107, 12080001: 108, 12090001: 109, 12100001: 110, 12110001: 111, 12120001: 112, 12130001: 113,
    12140001: 114, 12150001: 115, 12160001: 116, 12170001: 117, 12180001: 118, 12190001: 119, 12200001: 120, 12210001: 121,
    12220001: 122, 12230001: 123, 12240001: 124, 12250001: 125, 12260001: 126, 12270001: 127, 12280001: 128, 12290001: 129,
    12300001: 130, 12310001: 131, 12320001: 132, 12330001: 133, 12340001: 134, 19030001: 47, 19030002: 48, 19030003: 49,
    19030004: 50, 19030005: 51, 19030006: 52, 19030007: 53,
}


def map_kitchen_cooked(save, protos, enabled=True):
    """把已解锁的厨房配方料理标记成「已做过」，消除旅行百科食物格右上角的问号。

    必须在 map_encytravel 之后跑（依赖它写好的 encytravel.unlock）。
    只动 kitchen.cooked，不动已解锁列表、说明行数、展示款式；cooked 里原有的条目保留。
    写入值是布尔 true（游戏用 === true 判断），键是料理 item_id。
    """
    if not enabled:
        return '跳过（--no-cooked）'
    et = save.get('encytravel') or {}
    unlock = et.get('unlock') or []
    if not unlock:
        return '跳过：没有 encytravel.unlock'
    kitchen = save.get('kitchen')
    if not isinstance(kitchen, dict):
        kitchen = {}
    cooked = kitchen.get('cooked')
    if not isinstance(cooked, dict):
        cooked = {}
    old = sum(1 for v in cooked.values() if v is True)
    n = 0
    for long_id in unlock:
        item = RECIPE_LONG_TO_ITEM.get(int(long_id))
        if item is None or cooked.get(str(item)) is True:
            continue
        cooked[str(item)] = True
        n += 1
    kitchen['cooked'] = cooked
    save['kitchen'] = kitchen
    total = sum(1 for v in cooked.values() if v is True)
    return '厨房已做料理 %d -> %d 道（新标记 %d，百科问号消除）' % (old, total, n)


def map_encytravel(save, protos):
    """旅行百科：解锁、展示款式、说明行数三项一起搬（2026-11-26 修「百科不全」）。

    单机版 offline-encytravel.js 自己下发协议：
      unlock_list = encytravel.unlock
      show_sub    = 每组 encytravel.show[组号]，没写就取该组最小的解锁 id
      unlock_desc = 按 encytravel.received[组号] 推已开放行数
    之前只写 unlock，丢掉 show_sub / unlock_desc，导致：
      - 玩家在官服选过的款式回退到第一个（如 2101 铃铛第 5 款变第 1 款）
      - 说明行数回退到 1 行（官服开了 4 行也只显示 1 行）
    所以这里把 show / received 一起从官服协议复原。
    """
    d = data_of(protos, 'encytravel.load')
    if not (d or {}).get('unlock_list'):
        return '跳过：没有 encytravel.load'
    ref = save.get('encytravel') or {}
    old = ref.get('unlock') or []
    unlock = list(d['unlock_list'])
    ref['unlock'] = unlock
    msg = ['解锁 %d -> %d' % (len(old), len(unlock))]
    uset = set(unlock)

    # show_sub：每组官服当前展示的款式（long id），必须是已解锁的
    show = ref.get('show') if isinstance(ref.get('show'), dict) else {}
    nshow = 0
    for row in (d.get('show_sub') or []):
        if not isinstance(row, dict):
            continue
        g, sub = row.get('id'), row.get('sub_id')
        if g is None or sub is None or sub not in uset:
            continue
        if show.get(str(g)) != sub:
            nshow += 1
        show[str(g)] = sub
    if nshow:
        ref['show'] = show
        msg.append('展示款式 %d 组' % nshow)

    # unlock_desc：官服开了几行说明，反推 received。
    # 单机规则：第 i 行要求 received >= 1 + 3*(i-1)，所以最大行 m -> 1+3*(m-1)
    received = ref.get('received') if isinstance(ref.get('received'), dict) else {}
    nrec = 0
    for row in (d.get('unlock_desc') or []):
        if not isinstance(row, dict):
            continue
        g = row.get('id')
        lines = [int(x) for x in (row.get('list') or [])
                 if isinstance(x, int) and not isinstance(x, bool) and x > 0]
        if g is None or not lines:
            continue
        want = 1 + 3 * (max(lines) - 1)
        k = str(g)
        if (int(received.get(k) or 0) < want):
            received[k] = want
            nrec += 1
    if nrec:
        ref['received'] = received
        msg.append('说明行 %d 组' % nrec)

    save['encytravel'] = ref
    return '旅行百科 ' + '；'.join(msg)



def map_ency(save, protos):
    d = data_of(protos, 'encyclopedia.load')
    if not (d or {}).get('unlock_list'):
        return '跳过：没有 encyclopedia.load'
    ref = save.get('ency') or {}
    old = ref.get('unlock') or []
    ref['unlock'] = d['unlock_list']
    save['ency'] = ref
    return '百科解锁 %d -> %d' % (len(old), len(ref['unlock']))


def _museum_collection_ids():
    """从游戏配置 museumData_json.json 读五馆藏品 id collection_id（34..53 等），不写死。"""
    path = os.path.join(ROOT, 'data', 'config_json', 'museumData_json.json')
    ids = set()
    try:
        data = json.load(open(path, encoding='utf-8'))
        for m in (data.values() if isinstance(data, dict) else data):
            if not isinstance(m, dict):
                continue
            for v in str(m.get('collection_id') or '').split(','):
                v = v.strip()
                if v.isdigit():
                    ids.add(int(v))
    except Exception:
        pass
    return ids


def map_handbook(save, protos, union_collections=False, museum_collections=False):
    """图鉴 + 特产。

    默认只用官服协议真实点亮的 id，不掺其他存档数据。
    --museum-collections：仅额外补博物馆藏品（museumData 的 collection_id，五馆各 4 件），
        让游戏博物馆页不显示问号；日本藏品 304xxx 和其他底稿 id 仍不补，保持真实。
    --union-collections：并入整个 v37 底稿充数（不推荐）。
    """
    d = data_of(protos, 'item.load_handbook')
    if not d:
        return '跳过：没有 item.load_handbook'
    museum_ids = _museum_collection_ids() if museum_collections else set()
    msg = []
    for proto_key, save_key, label in (('collections', 'collections', '图鉴'),
                                       ('specialtys', 'specialtys', '特产')):
        got = d.get(proto_key)
        if not isinstance(got, list) or not got:
            msg.append('%s：协议空' % label)
            continue
        old = save.get(save_key) or []
        base = set(old) if isinstance(old, list) and all(isinstance(x, int) for x in old) else set()
        gotset = {x for x in got if isinstance(x, int)}
        if union_collections:
            keep = sorted(base | gotset)          # 并整个底稿
            mode = '并入底稿'
        else:
            # 真实点亮 + 可选博物馆藏品（仅在底稿存在的合法 id 内）
            keep = [x for x in got if isinstance(x, int) and (not base or x in base)]
            if save_key == 'collections' and museum_ids:
                added = sorted((museum_ids & base) - set(keep))
                keep = sorted(set(keep) | (museum_ids & base))
                msg.append('博物馆藏品补 %d 件' % len(added))
            mode = '官服真实+博物馆' if (save_key == 'collections' and museum_ids) else '仅官服真实'
        save[save_key] = keep
        msg.append('%s %d -> %d（协议 %d，%s）' % (label, len(old), len(keep), len(gotset), mode))
    return '；'.join(msg)


def map_museum_from_proto(save, protos, enabled=False):
    """用 museum_load 协议的真实数据替换存档里的博物馆藏品段。

    一键抓包时会自动进游戏博物馆页，服务器下发 museum_load，其 museum_list[].collections
    就是账号真实点亮的博物馆藏品 id（如江西省博物馆只点亮 id34）。开启本模块后：
    先移除存档里所有博物馆段 id（34-53 等），再写入协议真实点亮的，做到"官服有几个就补几个"。
    没有协议或没开启时不动（此时由 map_handbook 的 --museum-collections 兜底补全量）。
    """
    if not enabled:
        return '跳过（未开启 --museum-from-proto）'
    # 服务器实际回包的 cmd 名可能是 museum_load，也可能是未注册的 unknown_NN，
    # 所以按 data 里的 museum_list 特征找，不认死 cmd 名。
    ml = None
    for cmd, o in protos.items():
        dd = (o or {}).get('data')
        if isinstance(dd, dict) and isinstance(dd.get('museum_list'), list):
            ml = dd['museum_list']
            break
    if ml is None:
        return '跳过：没有 museum_list 协议（抓包时没进博物馆页）'
    lit = set()
    for m in ml:
        if isinstance(m, dict):
            for c in (m.get('collections') or []):
                if isinstance(c, int):
                    lit.add(c)
    museum_ids = _museum_collection_ids()
    lit &= museum_ids                      # 只保留合法博物馆藏品 id
    old = save.get('collections') or []
    base = set(x for x in old if isinstance(x, int))
    had = base & museum_ids
    save['collections'] = sorted((base - museum_ids) | lit)
    return ('博物馆真实点亮 %d 件 %s（替换原存档博物馆段 %d 件）'
            % (len(lit), sorted(lit), len(had)))


def map_story(save, protos):
    """故事/旅伴：story.load.stories 与 storyBook.list 同构（id/name/gift 三键）。"""
    d = data_of(protos, 'story.load')
    lst = (d or {}).get('stories')
    if not isinstance(lst, list) or not lst:
        return '跳过：没有 story.load'
    ref = save.get('storyBook') or {}
    old = ref.get('list') or []
    base = {x.get('id') for x in old if isinstance(x, dict)}
    got = [{'id': x.get('id'), 'name': x.get('name'), 'gift': x.get('gift')}
           for x in lst if isinstance(x, dict) and x.get('id') is not None]
    extra = [x['id'] for x in got if base and x['id'] not in base]
    if extra:
        # 协议里有底稿没有的故事 id：说明底稿不全，照收（游戏按 id 渲染，多出来只会是"新故事"）
        pass
    # 按 id 排序，和底稿一致，避免列表顺序影响 UI
    got.sort(key=lambda x: x['id'])
    ref['list'] = got
    save['storyBook'] = ref
    return '故事 %d -> %d%s' % (len(old), len(got),
                               '（新增底稿外 id %s）' % extra if extra else '')


def map_clovers(save, protos):
    """三叶草田：只换品种(element/sprite)与重生节奏，保留底稿的 grownAt 相对相位。"""
    lst = data_of(protos, 'clover.load_clovers')
    if not isinstance(lst, list) or not lst:
        return '跳过：没有 clover.load_clovers'
    old = save.get('clovers') or []
    by_id = {x.get('clover_id'): x for x in old if isinstance(x, dict)}
    now = int(time.time())
    out, changed = [], 0
    for x in lst:
        if not isinstance(x, dict) or x.get('clover_id') is None:
            continue
        cid = x['clover_id']
        base = dict(by_id.get(cid) or {})
        item = {'clover_id': cid,
                'element': x.get('element', base.get('element', 0)),
                'sprite': x.get('sprite', base.get('sprite', 1)),
                'last_harvest': x.get('last_harvest', base.get('last_harvest', now)),
                'rebirth_span': x.get('rebirth_span', base.get('rebirth_span', 3600)),
                'grownAt': base.get('grownAt')}
        # 保相位：底稿是"种下 -> 收割"的时长，平移过来，别让新时间戳对不上 grownAt
        if base.get('grownAt') and base.get('last_harvest') and item['grownAt'] is not None:
            gap = base['last_harvest'] - base['grownAt']
            if 0 < gap < 86400:
                item['grownAt'] = item['last_harvest'] - gap
        if item['grownAt'] is None:
            item['grownAt'] = item['last_harvest']
        # 收割时间落在未来会让整块田卡住不收，夹到"刚好成熟"
        if item['last_harvest'] + item['rebirth_span'] > now:
            item['last_harvest'] = now - item['rebirth_span']
        if item != base:
            changed += 1
        out.append(item)
    save['clovers'] = out
    sp = sorted({x['sprite'] for x in out})
    return '三叶草 %d -> %d（sprite 品种 %s，%d 株有变化）' % (len(old), len(out), sp, changed)


def map_garden(save, protos):
    """庭院三叶草地旁的花盆（3 个种植位）——注意这不是床边花瓶！
    床边花瓶插花是顶层 decoration.has / putId，见 map_decoration()。
    单机游戏下发花盆时用的是 garden.plants
    （offline-garden.js 的 flowerpotFields() 从 plants(draft) 重新生成 plant_list），
    存档里 furniture.extras.flowerpot.plant_list 会被忽略。所以必须把官服
    furniture.load_flowerpot.plant_list 还原成 garden.plants，否则花盆的品种/位置/数量全乱：
      - 官服 id = seed*100 + variant（如 2010903 -> seed 20109, variant 3）
      - index 是种植位（1 玩家 / 2 好友 / 3 青蛙），stage 是生长阶段，照搬
      - id<=0 是空种植位，不生成花株；但协议存在就一律以官服为准（清空底稿残留），
        否则官服是空花瓶、底稿那株花会冒出来，正是"花乱"的来源
      - growth 按 stage 给域内值；lastAt 设当前，避免被平移到未来冻住生长
    """
    fp = data_of(protos, 'furniture.load_flowerpot')
    plist = (fp or {}).get('plant_list') if isinstance(fp, dict) else None
    if not isinstance(plist, list):
        return '跳过：没有花瓶 plant_list（保留底稿花株）'
    now = int(time.time())
    out, skipped = [], 0
    for p in plist:
        if not isinstance(p, dict):
            skipped += 1
            continue
        pid = p.get('id')
        idx = p.get('index')
        if not isinstance(pid, int) or pid <= 0 or not isinstance(idx, int):
            skipped += 1  # id=0 空位正常跳过，不算异常
            continue
        seed, variant = divmod(pid, 100)
        stage = p.get('stage') if isinstance(p.get('stage'), int) else 1
        stage = max(1, min(3, stage))
        # growth：stage1 <56, stage2 56..112, stage3 >=112（草本 need=54，留余量）
        growth = 0 if stage == 1 else (60 if stage == 2 else 120)
        out.append({'index': idx, 'id': pid, 'seed': seed, 'variant': variant,
                    'stage': stage, 'growth': growth, 'lastAt': now})
    old = (save.get('garden') or {}).get('plants') or []
    save.setdefault('garden', {})['plants'] = out  # 含 out=[]（官服空花盆，清掉底稿残留）
    detail = ' '.join('%d:位%d/%d阶段' % (x['id'], x['index'], x['stage']) for x in out) or '空花盆'
    return '庭院花盆花株 %d -> %d（%s）' % (len(old), len(out), detail)


def map_decoration(save, protos, with_decoration=True):
    """床边花瓶插花（2026-11-26）：真源是顶层 decoration.has（各花种的数量）
    与 decoration.putId（当前插在瓶里展示的那朵花 id）。
    官服 client.load_decorate 协议下发 has_list=[{id,num}...] + put_id，
    单机游戏房间里的花瓶就照 decoration 显示。底稿残留的 has 数量是错的
    （例如水仙 105=51、狗尾草 1001=1，与官服 1001=150 完全不符），这正是
    用户说的「花瓶里的花数量会错乱」，所以协议里有就整体替换，不保留底稿。
      - 只收正整数 id / 正整数 num，过滤脏数据
      - has 的 key 用字符串（存档里就是字符串键：{"105": 51}）
      - put_id 只在协议明确给出时覆盖；status/autoRollAt 是展示状态/轮换时间，照搬
    """
    if not with_decoration:
        return '跳过（未开启 --with-decoration）'
    d = data_of(protos, 'client.load_decorate')
    if not isinstance(d, dict):
        return '跳过：没有 client.load_decorate（保留底稿插花）'
    has_list = d.get('has_list')
    if not isinstance(has_list, list):
        return '跳过：client.load_decorate 没有 has_list'
    has = {}
    for it in has_list:
        if not isinstance(it, dict):
            continue
        iid, num = it.get('id'), it.get('num')
        if isinstance(iid, bool) or not isinstance(iid, int) or iid <= 0:
            continue
        if isinstance(num, bool) or not isinstance(num, int) or num <= 0:
            continue
        has[str(iid)] = num
    dec = save.get('decoration')
    if not isinstance(dec, dict):
        dec = {}
        save['decoration'] = dec
    old = dec.get('has') if isinstance(dec.get('has'), dict) else {}
    old_put = dec.get('putId')
    dec['has'] = has                       # 整体替换，清掉底稿残留的错误数量
    if isinstance(d.get('put_id'), int) and not isinstance(d.get('put_id'), bool):
        dec['putId'] = d['put_id']
    if isinstance(d.get('status'), int):
        dec['status'] = d['status']
    if isinstance(d.get('autoRollAt'), int):
        dec['autoRollAt'] = d['autoRollAt']
    detail = ' '.join('%s×%s' % (k, v) for k, v in has.items())
    return '床边花瓶插花 %d 种 -> %d 种（putId %s -> %s；%s）' % (
        len(old), len(has), old_put, dec.get('putId'), detail[:120])


def map_capsule(save, protos):
    """扭蛋：只有货币位与协议同义，machine/rewards 是两套语义（机内球 vs 已得奖），不动。"""
    d = data_of(protos, 'capsule.load')
    if not d:
        return '跳过：没有 capsule.load'
    ref = save.get('capsule') or {}
    msg = []
    for pk, sk in (('coin', 'coin'), ('pre_coin', 'preCoin')):
        if pk in d and isinstance(d[pk], int) and ref.get(sk) != d[pk]:
            msg.append('%s %s -> %s' % (sk, ref.get(sk), d[pk]))
            ref[sk] = d[pk]
    save['capsule'] = ref
    return '；'.join(msg) or '货币位无变化（机内球/奖品语义不同，保留底稿）'


def map_animpicture(save, protos):
    """动态照片：页进度取官服协议，照片内容按页 id 保留底稿/协议里较全的一份。

    2026-11-26 二次修正：单机 offline-animpicture.js 的 payload() 是
    `pictures: p.pictures.map(clientPic)` —— 协议里的 pictures 就是存档页内照片的
    直接映射；官服下发 `pictures:{}` 表示该页没有照片（不是「未下发」）。
    但底稿 v37 的页 id=2 自带 2 张真照片，若整段照搬协议会把它们清掉。
    所以：
      - 页列表、phase/exp/put_num/id 用协议（账号真实制作进度）
      - pictures / exp_pic / base / returned 按页 id 对齐，谁的照片多留谁
      - 协议有而底稿没有的页照建，底稿有而协议没有的页保留
    """
    d = data_of(protos, 'animpicture.load')
    if not d:
        return '跳过：没有 animpicture.load'
    ref = save.get('animPicture') or {}
    msg = []
    for pk, sk in (('guide', 'guide'), ('item_num', 'itemNum'), ('making_index', 'makingIndex')):
        if pk in d and isinstance(d[pk], int):
            if ref.get(sk) != d[pk]:
                msg.append('%s %s -> %s' % (sk, ref.get(sk), d[pk]))
            ref[sk] = d[pk]

    def _pics(p):
        v = p.get('pictures') if isinstance(p, dict) else None
        if isinstance(v, list):
            return v
        if isinstance(v, dict):          # 官服空 dict == 空列表
            return []
        return []

    def _expics(p):
        v = p.get('exp_pic') if isinstance(p, dict) else None
        return v if isinstance(v, list) else ([] if isinstance(v, dict) else [])

    src = d.get('pic_list')
    old_pages = ref.get('pages') or []
    if isinstance(src, list) and src:
        old_by_id = {}
        for x in old_pages:
            if isinstance(x, dict) and x.get('id') is not None:
                old_by_id[x['id']] = x
        pages = []
        for p in src:
            if not isinstance(p, dict):
                continue
            q = dict(p)                                  # 协议页：进度以官服为准
            # 关键：官服空 {} 必须规范化成 []。单机 offline-animpicture.js 的
            # payload() 直接 p.pictures.map()，留 {} 会抛 TypeError 中断整个
            # client_load_all_info 推送，动态相册「一个都不显示」。
            q['pictures'] = _pics(q)
            q['exp_pic'] = _expics(q)
            o = old_by_id.get(p.get('id'))
            if o:
                # 照片内容取较全的一份（底稿 id=2 有 2 张真照片，官服是空 {}）
                if len(_pics(o)) > len(_pics(q)):
                    q['pictures'] = o.get('pictures')
                if len(_expics(o)) > len(_expics(q)):
                    q['exp_pic'] = o.get('exp_pic')
                for k in ('base', 'returned'):           # 协议不下发，沿用底稿
                    if k in o and k not in q:
                        q[k] = o[k]
            pages.append(q)
        # 底稿有、协议没有的页：保留（不删玩家已有内容）
        have = {p.get('id') for p in pages if isinstance(p, dict)}
        pages.extend([x for x in old_pages
                      if isinstance(x, dict) and x.get('id') not in have])
        added = sum(len(_pics(p)) for p in pages)
        ref['pages'] = pages
        msg.append('动态照片 %d 页 -> %d 页（%d 张）' % (len(old_pages), len(pages), added))
    if isinstance(d.get('page_num'), int):
        ref['pageNum'] = max(int(ref.get('pageNum') or 0), int(d['page_num']), len(ref.get('pages') or []))
    save['animPicture'] = ref
    return '；'.join(msg) or '标量无变化（页内照片保留 %d 页）' % len(ref.get('pages') or [])
def map_calendar(save, protos):
    """日历：只换"新标记"和当月幸运日，任务/签到等语义不明的字段不动。"""
    d = data_of(protos, 'calendar.load')
    if not d:
        return '跳过：没有 calendar.load'
    ref = save.get('calendar') or {}
    msg = []
    nf = d.get('new_flag')
    old = ref.get('newFlag')
    if isinstance(nf, list) and isinstance(old, list) and len(nf) == len(old):
        ref['newFlag'] = [1 if x else 0 for x in nf]
        msg.append('newFlag %s -> %s' % (old, ref['newFlag']))
    lucky = ref.get('lucky')
    ld = d.get('lucky_days')
    if isinstance(lucky, dict) and isinstance(ld, list):
        days = {}
        for x in ld:
            if isinstance(x, dict) and x.get('day') is not None and x.get('item_id'):
                days[str(x['day'])] = x['item_id']
        if days:
            old_days = lucky.get('days') or {}
            lucky['days'] = days
            msg.append('幸运日 %d -> %d 天' % (len(old_days), len(days)))
    save['calendar'] = ref
    return '；'.join(msg) or '无可用字段'


def map_greetcard(save, protos, enabled=True):
    """节日趣闻（收到的贺卡）：官服 greetcard.load.get_list → v14 存档顶层 receivedCards。

    v14（release_v14）改了读取位置：offline-greetcard.js 的 received() 只读顶层
    draft.receivedCards，字段 {kind, bg, bless, tags[3], name, gift}；
    v13.1 的「贺卡补丁版」读的是 greetCard.receivedList，v14 完全不认那个字段。
    这里两份都写：receivedCards 给 v14，receivedList 给旧补丁版，互不干扰。

    细节（对齐 v14 offline-official-merge.js 的贺卡并入逻辑）：
      · 去掉 uid/partner/account/player/name 之外的隐私字段？这里反过来——name 保留官服真实
        昵称（本机自己看的存档，stranger 名字照留），uid 不写进 v14 条目（原版也不存）。
      · gift 是回礼礼物 id（>0 表示已回礼）；v14 的 sendGift 靠它判断能否再送礼。
      · kind 固定 'mid'（中秋）；春节卡走 springcard.load，官服这两张表目前不混。
      · 抓到的 get_list 有时是空 dict {}（账号没收到过卡），有时是 list；两种都要能处理。
      · 如果两张卡 uid 相同但 bg/gift 不同仍算两张（官服同一蛙可能寄多次）。
    """
    if not enabled:
        return '跳过：--no-greetcard'
    d = data_of(protos, 'greetcard.load')
    got = (d or {}).get('get_list')
    if isinstance(got, dict):          # 空 {} 表示没收到过贺卡；非空 dict 取 value 当列表
        got = list(got.values()) if got else []
    if not isinstance(got, list) or not got:
        return '跳过：没有 greetcard.load.get_list（账号没收到过贺卡）'
    out, old_fmt, seen = [], [], set()
    for c in got:
        if not isinstance(c, dict):
            continue
        tags = c.get('tags') if isinstance(c.get('tags'), list) else []
        try:
            row = {
                'gift': int(c.get('gift') or 0),
                'uid': int(c.get('uid') or 0),
                'bg': int(c.get('bg') or 0),
                'name': str(c.get('name') or ''),
                'bless': int(c.get('bless') or 0),
                'tags': [int(x) for x in tags[:3]],
            }
        except (TypeError, ValueError):
            continue
        while len(row['tags']) < 3:
            row['tags'].append(0)
        ident = (row['uid'], row['bg'], row['gift'], tuple(row['tags']))
        if ident in seen:
            continue
        seen.add(ident)
        old_fmt.append(row)                                  # v13.1 补丁版：带 uid
        out.append({
            'kind': 'mid',
            'bg': row['bg'], 'bless': row['bless'],
            'tags': row['tags'][:], 'name': row['name'], 'gift': row['gift'],
        })                                                   # v14：顶层 receivedCards
    if not out:
        return '跳过：贺卡列表无有效条目'

    prev = save.get('receivedCards')
    n_old = len(prev) if isinstance(prev, list) else 0
    save['receivedCards'] = out                              # v14 主入口
    save['receivedCardNew'] = 1                              # 新卡红点，进游戏能立刻看到
    if isinstance(save.get('officialFestivalCards'), list):
        save.pop('officialFestivalCards', None)              # 旧版并入字段，留着会触发重复转换

    card = save.get('greetCard')
    if not isinstance(card, dict):
        card = {}
    card['receivedList'] = old_fmt                           # v13.1 贺卡补丁版兼容
    save['greetCard'] = card
    return '节日趣闻 %d -> %d 张（v14 receivedCards + v13.1 receivedList）' % (n_old, len(out))


def map_party(save, protos):
    """派对绘本/涂鸦本：数据源是 guest.load_drawing（不是 partycake，那是做蛋糕活动）。

    存档 party 与协议字段几乎一一对应：
      pages/colls/bag/state/guest 直接换；pen 保留底稿（协议给的是 pen_motion="write"，
      底稿是 "write4"，两套命名，硬换会让画笔样式变默认）。
    """
    d = data_of(protos, 'guest.load_drawing')
    if not d:
        return '跳过：没有 guest.load_drawing'
    ref = save.get('party') or {}
    msg = []
    for k in ('pages', 'colls', 'bag', 'state', 'guest'):
        if k in d and isinstance(d[k], (list, int)):
            old = ref.get(k)
            ref[k] = d[k]
            if isinstance(d[k], list):
                msg.append('%s %d -> %d' % (k, len(old or []), len(d[k])))
    if d.get('pen_motion') and not ref.get('pen'):
        ref['pen'] = d['pen_motion']
    save['party'] = ref
    return '；'.join(msg) or '无可用字段'


def map_album(save, protos, enable=False):
    """相册。

    album.load_all 只给 id+pic_id，图层得单独向 album.load_by_id_list 要
    （ws_proxy.py --inject-album 注入请求抓的，见 lib/抓取包/输出）。
    三档数据源，按可信度从高到低：
      1. album.load_by_id_list  服务端存的真图层，直接用（245/245）；
      2. album.load_new         相册外的新照片（带图层），id 不一定在相册里；
      3. 底稿同底图的照片        借贴纸布局，真图层抓不到时兜底。
    """
    d = data_of(protos, 'album.load_all')
    ids = (d or {}).get('id_list')
    if not ids:
        return '跳过：没有 album.load_all'
    full = {}
    for x in ((data_of(protos, 'album.load_by_id_list') or {}).get('pic_list') or []):
        if isinstance(x, dict) and x.get('id') is not None and x.get('layers'):
            full[x['id']] = x
    if not enable and not full:
        return ('跳过：只有 id+pic_id 且没抓到图层（%d 张）。'
                '先跑 ws_proxy.py --inject-album 补 album.load_by_id_list，'
                '或加 --with-album 借底稿' % len(ids))

    ref = save.get('pictures') or []
    fresh = {p.get('id'): p for p in ((data_of(protos, 'album.load_new') or {}).get('pictures') or [])}
    by_pic = {}
    # 底稿同 pic_id 的样板先入池
    for x in ref:
        if not isinstance(x, dict) or not x.get('layers'):
            continue
        cur = by_pic.get(x.get('pic_id'))
        if cur is None or len(x['layers']) > len(cur['layers']):
            by_pic[x.get('pic_id')] = x
    # 2026-11-26：把 album.load_new 里带图层的照片也按 pic_id 入池，
    # 以及补抓到的全量图层——同一 pic_id 的构图一致，可以互相兜底，减少空白照片。
    for pool in (fresh.values(), full.values()):
        for x in pool:
            if not isinstance(x, dict) or not x.get('layers'):
                continue
            cur = by_pic.get(x.get('pic_id'))
            if cur is None or len(x['layers']) > len(cur['layers']):
                by_pic[x.get('pic_id')] = x
    by_id_ref = {x.get('id'): x for x in ref if isinstance(x, dict)}
    fb = [{'layer': [0, 0, 0]}]
    now = int(time.time())
    rows = sorted([x for x in ids if isinstance(x, dict) and x.get('id') is not None],
                  key=lambda x: x['id'])
    out, stat = [], {'full': 0, 'new': 0, 'borrow': 0, 'bare': 0}
    for i, x in enumerate(rows):
        pid, pic = x['id'], x.get('pic_id')
        tpl = by_pic.get(pic)
        me = by_id_ref.get(pid)
        if pid in full:
            layers, src = full[pid]['layers'], 'full'
        elif fresh.get(pid) and fresh[pid].get('layers'):
            layers, src = fresh[pid]['layers'], 'new'
        elif tpl:
            layers, src = tpl['layers'], 'borrow'
        else:
            layers, src = fb, 'bare'
        stat[src] += 1
        # 拍摄时间协议不给，按 id 顺序倒推（间隔 6 小时），看起来像正常相册
        t = ((me or {}).get('time') or (tpl or {}).get('time')
             or (now - (len(rows) - i) * 21600))
        out.append({
            'pic_id': pic,
            'id': pid,
            'layers': layers,
            # 服务端存下来的就是官方合成图层；借来的沿用样板；兜底标 False
            'officialLayers': True if src in ('full', 'new') else bool((tpl or {}).get('officialLayers')),
            'time': t,
            'companionId': (me or {}).get('companionId', -1),   # 协议不给，沿用底稿同 id，否则 -1
            'compositionVersion': 1 if src != 'bare' else 0,
            'for_ads': 0,
            'visit': False,
            'world': 'china',
        })
    save['pictures'] = out
    if 'albumDeleted' in save:
        save['albumDeleted'] = []
    return '相册 %d -> %d（真图层 %d / 新照片 %d / 借底图 %d / 纯底图 %d）' % (
        len(ref), len(out), stat['full'], stat['new'], stat['borrow'], stat['bare'])

def map_furniture(save, protos, real_fur=False, keep_tumbler=False):
    # build_furniture 期望 {cmd: data}，这里把 {"cmd":..,"data":..} 拆开
    fur_protos = {}
    for k, v in protos.items():
        if not k.startswith('furniture.load_'):
            continue
        fur_protos[k] = v.get('data') if isinstance(v, dict) and 'data' in v else v
    if not fur_protos:
        return '跳过：没有 furniture.load_*'
    fur_data = json.load(open(FUR.FUR_DATA, encoding='utf-8'))
    shop_cfg = json.load(open(FUR.SHOP_DATA, encoding='utf-8'))
    built, stat = FUR.build_furniture(fur_protos, save.get('furniture'), fur_data, shop_cfg,
                                      full_has=not real_fur)
    msg = ['has_fur=%d（%s）' % (len(built.get('has_fur') or []),
                               '账号真实持有' if real_fur else 'v37 全量 399'),
           'put_fur=%d' % len(built.get('put_fur') or []),
           'bench=%d' % sum(1 for x in (built.get('bench') or []) if x != -1),
           'shop=%d' % len((built.get('shop') or {}).get('shop_list') or [])]

    t = fur_protos.get('furniture.load_tumbler')
    if t and isinstance(t.get('tumbler_list'), list):
        ex = built.setdefault('extras', {})
        tb = dict(ex.get('tumbler') or {})
        old = len(tb.get('tumbler_list') or [])
        if keep_tumbler:
            msg.append('不倒翁 %d（保留底稿）' % old)
        else:
            for f in ('tumbler_list', 'show_index', 'replace_index'):
                if f in t:
                    tb[f] = t[f]
            ex['tumbler'] = tb
            msg.append('不倒翁 %d -> %d' % (old, len(tb.get('tumbler_list') or [])))

    save['furniture'] = built
    return '；'.join(msg)


def map_task(save, protos):
    """伴蛙前行任务：把官服 task.load 的真实进度落到存档 tasks。

    官服 tasks[] = {id, pro, is_reward} → 存档 progress[id] = pro、claimed[id] = is_reward
    官服 list[]   = {id, pro}           → 存档 list[id] = pro
    官服 task.load_list.reward[]        → 存档 rewards[id] = pro
    游戏 evaluate() 的 put() 是单调只增（Math.max），写入真实进度后不会被回滚。
    以底稿打底再覆盖，保住官服没下发的单机特有任务（如 901/902 庆典）。
    """
    d = data_of(protos, 'task.load')
    if not d:
        return '跳过：没有 task.load'
    ref = save.get('tasks')
    if not isinstance(ref, dict):
        return '跳过：存档没有 tasks 字段'
    msg = []

    got_tasks = d.get('tasks')
    if isinstance(got_tasks, list) and got_tasks:
        prog = dict(ref.get('progress') or {})      # 底稿打底
        claim = dict(ref.get('claimed') or {})
        n_reward = 0
        for x in got_tasks:
            if not isinstance(x, dict) or x.get('id') is None:
                continue
            k = str(x['id'])
            v = x.get('pro')
            if isinstance(v, (int, float)):
                prog[k] = int(v)
            if x.get('is_reward'):
                claim[k] = True
                n_reward += 1
        old_n = len(ref.get('progress') or {})
        ref['progress'] = prog
        ref['claimed'] = claim
        msg.append('主任务进度 %d -> %d 条（已领奖 %d）' % (old_n, len(prog), n_reward))

    got_list = d.get('list')
    if isinstance(got_list, list) and got_list:
        lm = dict(ref.get('list') or {})
        for x in got_list:
            if not isinstance(x, dict) or x.get('id') is None:
                continue
            v = x.get('pro')
            if isinstance(v, (int, float)):
                lm[str(x['id'])] = int(v)
        old_n = len(ref.get('list') or {})
        ref['list'] = lm
        msg.append('计划进度 %d -> %d 条' % (old_n, len(lm)))

    dl = data_of(protos, 'task.load_list')
    reward = (dl or {}).get('reward') if isinstance(dl, dict) else None
    if isinstance(reward, list) and reward:
        rw = dict(ref.get('rewards') or {})
        for x in reward:
            if not isinstance(x, dict) or x.get('id') is None:
                continue
            v = x.get('pro')
            if isinstance(v, (int, float)):
                rw[str(x['id'])] = int(v)
        ref['rewards'] = rw
        msg.append('奖励档 %d 条' % len(rw))

    save['tasks'] = ref
    return '；'.join(msg) if msg else '跳过：task.load 为空'

def map_moment(save, protos, with_moment=False):
    """美好瞬间：把官服 misc.moment_load 的已解锁列表落到存档 moments。

    官服 misc.moment_load.data.list = [已解锁瞬间 id...]（登录时下发；游戏只累加不清空）。
    底稿可能没有 moments 字段，这里从协议新建；有则与已有取并集，不丢单机档里已解锁的。
    只保留正整数 id，过滤掉可能的脏数据（负数/字符串/0）。
    需 --with-moment 开启，默认不动，避免某些登录态没下发该协议时误清空。
    """
    if not with_moment:
        return '跳过（未开启 --with-moment）'
    d = data_of(protos, 'misc.moment_load')
    if not d:
        return '跳过：没有 misc.moment_load'
    got = d.get('list')
    if not isinstance(got, list):
        return '跳过：misc.moment_load 没有 list'
    old = save.get('moments')
    old = [x for x in old if isinstance(x, int) and x > 0] if isinstance(old, list) else []
    merged = list(dict.fromkeys(old))          # 保序去重，底稿已有在前
    add = 0
    for v in got:
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            continue
        if v not in merged:
            merged.append(v)
            add += 1
    save['moments'] = merged
    return '美好瞬间 %d -> %d 个（官服下发 %d，新增 %d）' % (len(old), len(merged), len(got), add)

MODULES = [
    ('身份/货币', lambda s, p, a: map_role(s, p, a.keep_achieves)),
    ('三叶草田', lambda s, p, a: map_clovers(s, p)),
    ('庭院花盆', lambda s, p, a: map_garden(s, p)),
    ('床边花瓶', lambda s, p, a: map_decoration(s, p, not a.no_decoration)),
    ('仓库/桌面/背包', lambda s, p, a: map_items(s, p)),
    ('见闻', lambda s, p, a: map_notes(s, p)),
    ('故事/旅伴', lambda s, p, a: map_story(s, p)),
    ('伴蛙前行任务', lambda s, p, a: map_task(s, p)),
    ('美好瞬间', lambda s, p, a: map_moment(s, p, a.with_moment)),
    ('印章/祈愿', lambda s, p, a: map_handcraft(s, p)),
    ('旅行百科', lambda s, p, a: map_encytravel(s, p)),
    ('厨房已做', lambda s, p, a: map_kitchen_cooked(s, p, not a.no_cooked)),
    ('图鉴百科', lambda s, p, a: map_ency(s, p)),
    ('图鉴/特产', lambda s, p, a: map_handbook(s, p, a.union_collections, a.museum_collections)),
    ('博物馆真实藏品', lambda s, p, a: map_museum_from_proto(s, p, a.museum_from_proto)),
    ('绘本/涂鸦', lambda s, p, a: map_party(s, p)),
    ('节日趣闻', lambda s, p, a: map_greetcard(s, p, not a.no_greetcard)),
    ('扭蛋', lambda s, p, a: map_capsule(s, p)),
    ('动态照片', lambda s, p, a: map_animpicture(s, p)),
    ('日历', lambda s, p, a: map_calendar(s, p)),
    ('相册', lambda s, p, a: map_album(s, p, a.with_album)),
    ('家具', lambda s, p, a: map_furniture(s, p, a.real_fur, a.keep_tumbler)),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--proto', required=True, help='协议目录')
    ap.add_argument('--base', required=True, help='底稿存档（v37）')
    ap.add_argument('--out', required=True, help='输出路径')
    ap.add_argument('--real-fur', action='store_true')
    ap.add_argument('--museum-collections', action='store_true',
                    help='额外补博物馆藏品（museumData 的 collection_id），让博物馆页不显示问号')
    ap.add_argument('--museum-from-proto', action='store_true',
                    help='用 museum_load 协议的真实点亮数据替换博物馆藏品段（一键抓包会自动进博物馆页抓到）')
    ap.add_argument('--union-collections', action='store_true',
                    help='图鉴并入整个 v37 底稿充数（不推荐）')
    ap.add_argument('--keep-tumbler', action='store_true', help='不倒翁保留 v37 全量 156，不换成账号真实的 12')
    ap.add_argument('--with-album', action='store_true')
    ap.add_argument('--with-moment', action='store_true',
                    help='从 misc.moment_load 协议写入已解锁美好瞬间（登录态下发时才抓得到）')
    ap.add_argument('--keep-achieves', action='store_true')
    ap.add_argument('--no-cooked', action='store_true',
                    help='不自动把已解锁的配方料理标记为已做过（默认标记，消除百科右上角问号）')
    ap.add_argument('--no-greetcard', action='store_true',
                    help='不把 greetcard.load 的真实贺卡写入存档（默认写入，需配合贺卡补丁 APK）')
    ap.add_argument('--no-decoration', action='store_true',
                    help='不把 client.load_decorate 的真实床边花瓶插花写入存档（默认写入）')
    ap.add_argument('--plain', action='store_true')
    a = ap.parse_args()

    save, env = tf.load(a.base)
    protos = read_protos(a.proto)
    if not protos:
        print('[X] 没读到协议:', a.proto)
        return 1
    print('底稿: %s（顶层 %d 键）' % (os.path.basename(a.base), len(save)))
    print('协议: %d 个\n' % len(protos))

    print('=== 模块映射 ===')
    for name, fn in MODULES:
        try:
            msg = fn(save, protos, a)
        except Exception as e:
            msg = '[X] 异常 %s: %s' % (type(e).__name__, e)
        print('  %-12s %s' % (name, msg))

    now = int(time.time())
    save['lastSaveTime'] = now
    save['lastSaveTimeMs'] = now * 1000
    if env:
        env['exportedAt'] = now * 1000
        env['exportedAtText'] = time.strftime('%Y-%m-%d %H:%M', time.localtime(now))

    print('\n=== 关键量 ===')
    r = save.get('role') or {}
    print('  昵称 %r  三叶草 %s  抽奖券 %s  称号 %d'
          % (r.get('name'), r.get('clover'), r.get('ticket'), len(r.get('achieves') or [])))
    for k in ('house', 'notes', 'pictures', 'collections', 'specialtys', 'clovers'):
        print('  %-12s %d' % (k, len(save.get(k) or [])))
    print('  %-12s %d' % ('storyBook', len((save.get('storyBook') or {}).get('list') or [])))
    dec = save.get('decoration') or {}
    print('  %-12s %d 种 / 插 #%s' % ('床边花瓶', len(dec.get('has') or {}), dec.get('putId')))
    fu = save.get('furniture') or {}
    print('  furniture  %d 件 / 摆放 %d / 商店 %d'
          % (len(fu.get('has_fur') or []), len(fu.get('put_fur') or []),
             len((fu.get('shop') or {}).get('shop_list') or [])))

    if a.plain or not env:
        # 明文文件是"裸 save"，没有外层信封（缺 format 字段），游戏点导入会报 E3。
        # 自动给文件名加标记，避免和可导入的加密档混在一起选错。
        plain_out = a.out
        if '不可导入' not in plain_out:
            root, ext = os.path.splitext(plain_out)
            plain_out = root + '_明文不可导入_仅供查看' + (ext or '.json')
        json.dump(save, open(plain_out, 'w', encoding='utf-8'), ensure_ascii=False)
        print('\n写入（明文）:', plain_out)
        if plain_out != a.out:
            print('  ⚠ 文件名已自动改成带「不可导入」的版本。')
            print('    这个文件只是数据体，没有 format 字段，游戏里选它会报 E3。')
            print('    要导入请用同目录的加密版：%s' % a.out)
    else:
        tf.save(a.out, save, env['salt'], env)
        chk, _ = tf.load(a.out)
        print('\n写入（加密 v2）:', a.out)
        print('回读: 顶层 %d 键 / 昵称 %r / 三叶草 %s / 相册 %d'
              % (len(chk), (chk.get('role') or {}).get('name'), (chk.get('role') or {}).get('clover'),
                 len(chk.get('pictures') or [])))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
