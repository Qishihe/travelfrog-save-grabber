# -*- coding: utf-8 -*-
"""
补写官服注册时间到「已经抓好的存档」（v2.8）
============================================

背景
----
抓取器旧版本只把官服注册时间写进单机版 createTime（有的版本甚至没写），
没有写 officialCreateTime。编辑器「时间记录 → 注册时间 · 官服 / 单机版」
读的是 officialCreateTime，于是显示「官服未抓到 / 未设置 (0)」。

存档是加密的（data 为 base64+异或流，外层有 checksum），必须用 lib/tf.py
的 load/save 读写，直接改 JSON 外层会破坏校验、游戏无法导入。

本脚本不需要重新抓包：从抓包留下的 协议/client.load_role.json 读回
misc.create_time，按账号名匹配存档，补写进 officialCreateTime，重新加密并备份。

用法
----
  python _补写官服注册时间.py              扫描所有存档，自动匹配协议
  python _补写官服注册时间.py <路径>        只处理指定存档或目录
  python _补写官服注册时间.py <存档> --ct 1608260257
                                            手动指定官服注册时间（Unix 秒）

选项
----
  --dry    只预演，不写文件
  --force  即使已有 officialCreateTime 也覆盖
  --ct N   手动指定官服注册时间，跳过协议匹配
"""
import os
import sys
import json
import shutil
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TF_LIB = os.path.join(HERE, "07_绿色包_一键抓官服", "lib")
sys.path.insert(0, TF_LIB)
import tf  # noqa: E402

DEFAULT_ROOTS = [
    r"E:/BaiduNetdiskDownload/travelfrog/",
]
PROTO_NAME = "client.load_role.json"
FLAG = "officialCreateTime"


def log(*a):
    print(*a, flush=True)


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_proto(path):
    """返回 (账号名, create_time) 或 (None, None)。"""
    try:
        d = read_json(path)
    except Exception:
        return None, None
    node = d.get("data", d) if isinstance(d, dict) else None
    if not isinstance(node, dict):
        return None, None
    misc = node.get("misc")
    if not isinstance(misc, dict):
        res = node.get("res")
        if isinstance(res, dict):
            misc = res.get("misc")
    ct = None
    if isinstance(misc, dict):
        ct = misc.get("create_time")
    if ct is None:
        ct = node.get("create_time")
    try:
        ct = int(ct)
    except (TypeError, ValueError):
        ct = None
    if ct is not None and not (1000000000 < ct < 2000000000):
        ct = None
    # 账号名：协议里可能在 frog.name / role.name / misc.name
    name = None
    for holder in (node.get("frog"), node.get("role"), misc):
        if isinstance(holder, dict) and holder.get("name"):
            name = holder["name"]
            break
    return name, ct


def build_proto_index():
    """扫描所有 client.load_role.json，建 {账号名: [(ct, path), ...]}。"""
    idx = {}
    seen = set()
    for root in DEFAULT_ROOTS:
        if not os.path.isdir(root):
            continue
        for cur, dirs, files in os.walk(root):
            if PROTO_NAME not in files:
                continue
            full = os.path.join(cur, PROTO_NAME)
            key = os.path.normcase(os.path.abspath(full))
            if key in seen:
                continue
            seen.add(key)
            name, ct = parse_proto(full)
            if name and ct:
                idx.setdefault(name, []).append((ct, full))
    # 同名取路径时间戳最大的（最新抓的）
    for name in idx:
        idx[name].sort(key=lambda x: x[1], reverse=True)
    return idx


def is_save_file(name):
    low = name.lower()
    if not low.endswith(".json"):
        return False
    if "存档" not in name and "save" not in low:
        return False
    for bad in ("原始数据", "明文不可导入", "已消问号", "模板", ".bak"):
        if bad.lower() in low:
            return False
    return True


def load_save(path):
    """返回 (明文 dict, env)，失败返回 (None, None)。"""
    try:
        return tf.load(path)
    except Exception:
        return None, None


def role_name(save):
    role = save.get("role") if isinstance(save, dict) else None
    if isinstance(role, dict) and role.get("name"):
        return role["name"]
    return None


def collect_targets(arg):
    if arg:
        p = os.path.abspath(arg)
        if os.path.isfile(p):
            return [p]
        if os.path.isdir(p):
            out = []
            for root, dirs, files in os.walk(p):
                for n in files:
                    if is_save_file(n):
                        out.append(os.path.join(root, n))
            return out
        log("路径不存在:", arg)
        return []
    out = []
    for root in DEFAULT_ROOTS:
        if not os.path.isdir(root):
            continue
        for cur, dirs, files in os.walk(root):
            for n in files:
                if is_save_file(n):
                    out.append(os.path.join(cur, n))
    seen, uniq = set(), []
    for p in out:
        k = os.path.normcase(os.path.abspath(p))
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def main():
    args = sys.argv[1:]
    dry = "--dry" in args
    force = "--force" in args
    manual_ct = None
    if "--ct" in args:
        i = args.index("--ct")
        if i + 1 < len(args):
            try:
                manual_ct = int(args[i + 1])
            except ValueError:
                log("--ct 后面要跟 Unix 秒整数")
                return
    positional = []
    skip_next = False
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a == "--ct":
            skip_next = True
            continue
        if a.startswith("--"):
            continue
        positional.append(a)
    targets = collect_targets(positional[0] if positional else None)
    log("扫描到 %d 个存档候选" % len(targets))
    idx = {} if manual_ct else build_proto_index()
    log("协议索引账号数: %d  (%s)" % (len(idx), ", ".join(list(idx)[:8])))
    done = skipped = 0
    for sp in targets:
        save, env = load_save(sp)
        if not isinstance(save, dict):
            skipped += 1
            continue
        old = save.get(FLAG)
        if old and not force:
            log("  [已有] %s officialCreateTime=%s" % (os.path.basename(sp), old))
            skipped += 1
            continue
        ct = manual_ct
        src = "手动指定"
        if ct is None:
            nm = role_name(save)
            hits = idx.get(nm) if nm else None
            if hits:
                ct = hits[0][0]
                src = "%s (协议 %s)" % (nm, os.path.basename(hits[0][1]))
        if ct is None:
            log("  [跳过] %s 找不到官服协议 (账号=%s)" % (
                os.path.basename(sp), role_name(save)))
            skipped += 1
            continue
        save[FLAG] = ct
        ts = time.strftime("%Y-%m-%d", time.localtime(ct))
        if dry:
            log("  [预演] %s -> %s (%s) 来源=%s" % (os.path.basename(sp), ct, ts, src))
            done += 1
            continue
        bak = sp + ".bak_补写前"
        if not os.path.exists(bak):
            shutil.copy2(sp, bak)
        tf.save(sp, save, env['salt'], env)
        log("  [写入] %s -> %s (%s) 来源=%s" % (os.path.basename(sp), ct, ts, src))
        done += 1
    log("完成：写入/预演 %d，跳过 %d" % (done, skipped))


if __name__ == "__main__":
    main()
