# -*- coding: utf-8 -*-
"""一键恢复模拟器联网 —— 清除抓包残留的 iptables 劫持 + 还原系统证书目录。

为什么单独一个脚本：
    抓取时环境还原失败（异常退出、跨子进程 ALL_ON 标志失效等）会让模拟器
    所有 443 流量仍被劫持到已停止的代理，且系统根证书被替换成只剩自签 CA。
    表现就是「抓完存档后进不去游戏」：小米/华为 FlySDK 校验证书链失败
    （Trust anchor for certification path not found）卡在启动页死循环。
    这个脚本无条件、幂等地把环境清干净，网页「恢复联网」按钮调用它。

子命令：
    status   只检查残留（劫持规则数、证书挂载状态），不改动
    restore  清除残留并校验；可加 --reboot 让模拟器重启做终极兜底

依赖：adb + 模拟器 root（iptables / umount 需要）。只操作本机模拟器，不联网。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

HOST_PROXY = '10.0.2.2:8443'
CACERT_DIR = '/system/etc/security/cacerts'
TMP_DIR = '/data/local/tmp/cacerts'

try:
    import cert_install          # 检查 shell + zygote 各命名空间里的证书挂载
except Exception:
    cert_install = None
CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

ADB_CANDIDATES = [
    os.environ.get('ADB'),
    os.path.join(ROOT, 'tools', 'platform-tools', 'adb.exe'),
    r'D:\Program Files\Netease\MuMuPlayer-12.0\nx_main\adb.exe',
    r'D:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe',
    r'C:\Program Files\Netease\MuMuPlayer-12.0\nx_main\adb.exe',
    r'C:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe',
]
KNOWN_PORTS = ['16384', '16448', '16416', '7555', '5557', '5555', '62001', '21503']

ADB = [None]
SER = [None]


def run(cmd, timeout=90):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        out = (p.stdout or b'').decode('utf-8', 'replace')
        err = (p.stderr or b'').decode('utf-8', 'replace')
        return p.returncode, (out + ('\n' + err if err.strip() else '')).strip()
    except subprocess.TimeoutExpired:
        return 1, '超时 %ss' % timeout
    except Exception as e:
        return 1, str(e)


def find_adb():
    for c in ADB_CANDIDATES:
        if c and os.path.isfile(c):
            return c
    return shutil.which('adb')


def adb(*args, t=90):
    return run([ADB[0], '-s', SER[0]] + list(args), t)


def sh(cmd, t=90):
    return adb('shell', cmd, t=t)[1].strip()


def connect(port=None):
    if not ADB[0]:
        ADB[0] = find_adb()
    if not ADB[0]:
        return None
    run([ADB[0], 'start-server'], 20)
    ports = [str(port)] if port else KNOWN_PORTS
    fallback = None
    for p in ports:
        target = '127.0.0.1:%s' % p
        run([ADB[0], 'connect', target], 10)
        rc, out = run([ADB[0], '-s', target, 'shell', 'pm', 'list', 'packages'], 25)
        if rc != 0 or 'package:' not in out:
            continue
        if fallback is None:
            fallback = target
        # 优先挑装了旅行青蛙的模拟器
        if 'aligames' in out.lower() or 'croak' in out.lower():
            SER[0] = target
            return target
    SER[0] = fallback
    return fallback


def inspect():
    """只读检查残留情况（含 zygote/应用 mount namespace）。"""
    rule_cnt = sh("iptables -t nat -S OUTPUT 2>/dev/null | grep -c -- '--to-destination %s'" % HOST_PROXY)
    mounted = False
    if cert_install is not None:
        try:
            namespaces = [None] + cert_install.zygote_pids(sh)
            mounted = any(cert_install.mounted_targets(sh, TMP_DIR, pid)
                          for pid in namespaces)
        except Exception:
            mounted = False
    if not mounted:
        r = sh('mount | grep -c %s' % TMP_DIR)
        mounted = (r or '').strip() not in ('0', '')
    ca_cnt = sh('ls %s 2>/dev/null | wc -l' % CACERT_DIR)
    def _n(x):
        x = (x or '').strip()
        return int(x) if x.isdigit() else 0
    return {'ok': True,
            'rules': _n(rule_cnt),
            'ca_mounted': bool(mounted),
            'ca_count': _n(ca_cnt)}


def restore(reboot=False):
    try:
        import grab_diag
    except Exception as e:
        return {'ok': False, 'error': '无法加载 grab_diag：%s' % e}
    before = inspect()
    res = grab_diag.cleanup_all(sh, HOST_PROXY, CACERT_DIR, TMP_DIR)
    after = inspect()
    # 可选终极兜底：重启模拟器（MuMu 重启会清空 iptables 与 bind mount）
    if reboot and not res.get('cleaned'):
        run([ADB[0], '-s', SER[0], 'reboot'], 20)
        time.sleep(25)
        run([ADB[0], 'connect', SER[0]], 10)
        after = inspect()
    ok = after['rules'] == 0 and not after['ca_mounted']
    return {'ok': ok, 'before': before, 'after': after,
            'cleaned': res.get('cleaned'),
            'error': '' if ok else '环境未完全还原，建议点「重启模拟器」或手动重启'}


def main():
    ap = argparse.ArgumentParser(description='模拟器联网恢复（清抓包残留）')
    ap.add_argument('cmd', choices=['status', 'restore'], nargs='?', default='restore')
    ap.add_argument('--port', help='模拟器 adb 端口')
    ap.add_argument('--reboot', action='store_true', help='清理后仍不干净则重启模拟器兜底')
    a = ap.parse_args()

    if not connect(a.port):
        print(json.dumps({'ok': False, 'error': '连不上模拟器，请先启动 MuMu / 雷电'},
                         ensure_ascii=False))
        return 2
    res = inspect() if a.cmd == 'status' else restore(reboot=a.reboot)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
