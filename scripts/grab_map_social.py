# -*- coding: utf-8 -*-
"""独立守候抓包：地图点赞 / 评论留言 / 漂流瓶。

和 grab_official.py 的区别（很重要）：
  * 不 force-stop、不重启游戏、不模拟点击 —— 全程由人手动操作；
  * 先挂好证书 + DNAT + 代理，然后等用户手动进米服地图页；
  * 边抓边解析，实时报告命中的协议；结束写成一份可读报告。

用法：
    python scripts/grab_map_social.py --pkg 米服 --seconds 600
    python scripts/grab_map_social.py --port 16384 --pkg 米服 --seconds 600

结束方式：
  * 到 --seconds 秒自动结束；
  * 或新建 tools/mitm/STOP_CAPTURE 文件提前结束（脚本下一轮轮询会看到）。
"""
import argparse
import glob
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import grab_official as g          # 复用 adb/root/证书/DNAT/解析等成熟实现

STOP_FILE = os.path.join(g.MITM, 'STOP_CAPTURE')

# 关心的字段 -> 中文说明。既认标准 cmd，也认 unknown_NN 里的同名字段。
SOCIAL_KEYS = {
    'inspire': '点赞/鼓舞数', 'inspire_num': '点赞/鼓舞数',
    'inspire_time': '上次点赞时间', 'log_list': '评论留言',
    'comment': '评论', 'comments': '评论列表', 'drift': '漂流瓶',
    'bottle': '漂流瓶', 'footprint': '足迹', 'like': '点赞',
    'museum_list': '地图藏品', 'path': '地图格子', 'compass': '饼干数',
}


def wait_device(want_port, want_pkg, timeout=180):
    """等 MuMu 手动开机并连上 adb（米服实例可能要几十秒）。"""
    end = time.time() + timeout
    last = ''
    while time.time() < end:
        ser = g.detect_serial(want_port, want_pkg)
        if ser:
            return ser
        left = int(end - time.time())
        msg = '\r   等模拟器上线… 剩余 %3ds' % left
        if msg != last:
            sys.stdout.write(msg)
            sys.stdout.flush()
            last = msg
        time.sleep(3)
    print('')
    return None


def ensure_root():
    ident = g.sh('id')
    if 'uid=0' in ident:
        return True
    g.run([g.ADB[0], '-s', g.SER[0], 'root'], timeout=30)
    time.sleep(3)
    for _ in range(5):
        if 'uid=0' in g.sh('id'):
            return True
        time.sleep(1)
    return False


def start_proxy():
    g.free_proxy_port()
    cmd = [sys.executable, g.PROXY, '--listen', '8443', '--host', g.LOGIN_HOST]
    cmd.append('--host-all')   # 复用抓取器逻辑；SDK 域名已在 ws_proxy 透传名单中放行
    cmd.append('--inject-album')
    out_dir = os.path.join(ROOT, 'output', '地图社交补采_%s' % time.strftime('%Y%m%d_%H%M%S'))
    os.makedirs(out_dir, exist_ok=True)
    log = open(os.path.join(out_dir, '代理日志.txt'), 'w', encoding='utf-8')
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    time.sleep(2)
    if proc.poll() is not None:
        log.flush()
        print('[X] 代理启动失败：%s' % open(log.name, encoding='utf-8', errors='replace').read()[-300:])
        return None, out_dir, log
    print('   代理已启动（全域名解密），日志 → %s' % log.name)
    return proc, out_dir, log


def sessions_now():
    """当前 capture 下所有有效会话目录。"""
    out = []
    for d in glob.glob(os.path.join(g.CAP, '*')):
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, 's2c.bin')):
            out.append(d)
    return sorted(out)


def sniff_sessions(sessions, seen):
    """实时解析新会话，打印命中的社交字段。不写总目录，避免污染正式存档。"""
    hits = []
    for d in sessions:
        if d in seen or g.size_of_s2c(d) < 800:
            continue
        seen.add(d)
        tmp = os.path.join(d, '_proto')
        rc, _ = g.run([sys.executable, g.PARSE, d, tmp], timeout=60)
        if rc != 0:
            continue
        for f in sorted(glob.glob(os.path.join(tmp, '*.json'))):
            try:
                o = json.load(open(f, encoding='utf-8'))
            except Exception:
                continue
            data = o.get('data') if isinstance(o.get('data'), dict) else {}
            found = [k for k in SOCIAL_KEYS if k in data]
            if found:
                brief = []
                for k in found:
                    v = data[k]
                    n = len(v) if isinstance(v, (list, dict)) else v
                    brief.append('%s=%s' % (SOCIAL_KEYS[k], n))
                line = '   [%s] %s → %s' % (
                    time.strftime('%H:%M:%S'), o.get('cmd') or os.path.basename(f), ', '.join(brief))
                print(line, flush=True)
                hits.append((d, o.get('cmd') or os.path.basename(f), found))
    return hits


DRIFT_TYPE = 8      # Mail.EvtId.Drift


def build_report(sessions, out_dir):
    """解析所有会话，提取地图点赞/评论/漂流瓶，写 JSON + 文本报告。"""
    proto_dir = os.path.join(out_dir, '协议')
    os.makedirs(proto_dir, exist_ok=True)
    n = g.parse_sessions(sessions, proto_dir)

    def best_cmd(cmd):
        """同名取 data 键最多的那份（空壳登录包会覆盖真实数据包）。"""
        files = sorted(glob.glob(os.path.join(proto_dir, cmd + '.json')))
        best, score = None, -1
        for f in files:
            try:
                o = json.load(open(f, encoding='utf-8'))
            except Exception:
                continue
            d = o.get('data') or {}
            s = len(json.dumps(d, ensure_ascii=False))
            if s > score:
                best, score = o, s
        return best

    museum = best_cmd('museumday.load') or {}
    mail = best_cmd('mail.load') or {}
    md = museum.get('data') if isinstance(museum.get('data'), dict) else {}
    ml = mail.get('data') if isinstance(mail.get('data'), dict) else {}

    # 未知协议里可能藏着地图社交字段，扫一遍兜底
    other = []
    for f in sorted(glob.glob(os.path.join(proto_dir, '*.json'))):
        cmd = os.path.basename(f)[:-5]
        if cmd in ('museumday.load', 'mail.load'):
            continue
        try:
            o = json.load(open(f, encoding='utf-8'))
        except Exception:
            continue
        d = o.get('data') if isinstance(o.get('data'), dict) else {}
        keys = [k for k in SOCIAL_KEYS if k in d]
        if keys:
            other.append({'cmd': cmd, 'keys': keys, 'data': d})

    # 漂流瓶：mail.list 里 type=8 的邮件
    mails = ml.get('list') or ml.get('mail_list') or []
    if isinstance(mails, dict):
        mails = list(mails.values())
    drifts = [m for m in mails if isinstance(m, dict) and
              (m.get('type') == DRIFT_TYPE or 'drift' in str(m.get('evt', '')).lower())]

    report = {
        'captured_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'protocol_files': n,
        'map': {
            'inspire_num': md.get('inspire_num'),
            'inspire_time': md.get('inspire_time'),
            'compass': md.get('compass'),
            'log_list': md.get('log_list'),
            'museum_list': md.get('museum_list'),
            'path': md.get('path'),
            'task_num': md.get('task_num'),
        },
        'drift_bottles': drifts,
        'other_social_protocols': other,
    }
    jf = os.path.join(out_dir, '地图社交数据.json')
    with io.open(jf, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    lines = []
    lines.append('旅行青蛙 · 地图社交数据补采报告')
    lines.append('采集时间：%s' % report['captured_at'])
    lines.append('解析协议：%d 个 → %s' % (n, proto_dir))
    lines.append('')
    lines.append('【点赞 / 鼓舞】 %s' % (md.get('inspire_num') if md.get('inspire_num') is not None else '未抓到'))
    if md.get('inspire_time'):
        lines.append('  最后点赞时间：%s' % time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(md['inspire_time'])))
    lines.append('【评论留言 log_list】')
    logs = md.get('log_list')
    if isinstance(logs, dict):
        logs = list(logs.values())
    if logs:
        for x in logs:
            lines.append('  ' + json.dumps(x, ensure_ascii=False)[:200])
    else:
        lines.append('  未抓到（需在地图页点开留言列表）')
    lines.append('【漂流瓶】 %d 封' % len(drifts))
    for m in drifts:
        lines.append('  ' + json.dumps(m, ensure_ascii=False)[:300])
    if not drifts:
        lines.append('  未抓到（需在邮件里打开漂流瓶）')
    lines.append('【其它命中协议】')
    for o in other:
        lines.append('  %s: %s' % (o['cmd'], ', '.join(SOCIAL_KEYS[k] for k in o['keys'])))
    tf = os.path.join(out_dir, '地图社交数据.txt')
    with io.open(tf, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print('   报告 → %s' % jf)
    print('   报告 → %s' % tf)
    return report


def main():
    ap = argparse.ArgumentParser(description='守候抓地图点赞/评论/漂流瓶（不操作游戏）')
    ap.add_argument('--port', help='模拟器 adb 端口，如 16384')
    ap.add_argument('--serial', help='直接指定 adb serial（跳过探测，避免 adb 反复掉线时卡住）')
    ap.add_argument('--pkg', default='米服', help='包名或别名（默认 米服）')
    ap.add_argument('--seconds', type=int, default=600, help='守候秒数（默认 600）')
    a = ap.parse_args()

    print('=' * 68)
    print('  地图社交数据补采（手动模式）')
    print('=' * 68)

    g.ADB[0] = g.find_adb()
    if not g.ADB[0]:
        print('[X] 找不到 adb')
        return 1
    want_pkg = g.PKG_ALIAS.get(str(a.pkg).strip().lower(), a.pkg)
    if a.serial:
        ser = a.serial
        print('   直接用指定设备：%s' % ser)
    else:
        print('   等米服实例上线（你在 MuMu 里手动开机）…')
        ser = wait_device(a.port, want_pkg)
    if not ser:
        print('[X] 没等到装了 %s 的模拟器；确认 MuMu 已开机、装了这个服。' % want_pkg)
        return 1
    g.SER[0] = ser
    print('   设备：%s' % ser)

    if not ensure_root():
        print('[X] adb root 失败，请在模拟器设置里打开 root / ADB 调试')
        return 1
    print('   root：已就绪')

    if os.path.isfile(STOP_FILE) and os.path.getsize(STOP_FILE) > 0:
        try:
            os.remove(STOP_FILE)
        except Exception:
            io.open(STOP_FILE, 'w').close()  # 删不掉就清空，内容为空同样视为无停止信号
    try:
        pre = g.diag.cleanup_all(g.sh, g.HOST_PROXY, g.CACERT_DIR, g.TMP_DIR) if g.diag else None
        if pre and (pre.get('rules_before') or pre.get('mounted_after')):
            print('   已清理残留劫持规则 %s 条' % pre.get('rules_before'))
    except Exception:
        pass

    ok, msg = g.verify_cert_pair()
    print('   证书自检：%s' % msg)
    if not ok:
        return 1

    print('\n[1/4] 启动代理 + 装证书 + 重定向流量…')
    proc, out_dir, log = start_proxy()
    if not proc:
        return 1
    ips = g.resolve_ips()
    if not g.mitm_on(ips):
        try:
            proc.terminate()
        except Exception:
            pass
        return 1

    seen = set()
    # 复用抓取器的 restart_game（game_boot 智能识别启动页/协议勾选/进入游戏）
    print('\n[2/4] 用抓取器逻辑重开游戏并过启动页…')
    _stall = 40 if want_pkg in ('com.aligames.lxqw.hhb', 'com.ali.croak.mi') else 70
    sess = g.restart_game(want_pkg, clear=False, tap=(450, 1170),
                          timeout=a.seconds, stall=_stall)
    seen.update(sess)   # 已解析过的会话不再重复
    print('      登录会话 %d 个。现在请你在游戏里手动进地图。' % len(sess))

    print('\n[3/4] 守候中（%d 秒）…' % a.seconds)
    print('      请手动：地图页点赞、翻评论/留言；邮件里点开漂流瓶')
    print('      抓够后等它自动结束，或新建 %s 提前结束。' % STOP_FILE)

    hits = []
    end = time.time() + a.seconds
    next_tick = 0
    try:
        while time.time() < end:
            if os.path.isfile(STOP_FILE) and os.path.getsize(STOP_FILE) > 0:
                print('   收到停止信号，结束守候')
                try:
                    os.remove(STOP_FILE)
                except Exception:
                    pass
                break
            now = time.time()
            if now >= next_tick:
                hits += sniff_sessions(sessions_now(), seen)
                left = int(end - now)
                print('\r   守候中… 剩余 %3ds，已看到 %d 个会话 / %d 次命中' %
                      (left, len(seen), len(hits)), end='', flush=True)
                next_tick = now + 4
            time.sleep(1)
    except KeyboardInterrupt:
        print('\n   手动中断')
    print('')

    print('\n[4/4] 还原环境 + 停代理 + 解析…')
    g.sh('am force-stop %s' % want_pkg)
    g.mitm_off(ips)
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        log.close()
    except Exception:
        pass

    sessions = sessions_now()
    print('   会话 %d 个' % len(sessions))
    build_report(sessions, out_dir)
    print('   完成。输出目录：%s' % out_dir)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        print('[X] %s: %s' % (exc.__class__.__name__, exc))
        raise SystemExit(1)
