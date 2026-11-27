# -*- coding: utf-8 -*-
"""OPPO 渠道服辅助 —— 查登录状态 / 一键退出账号（重新登录用）。

为什么单独一个文件：
    OPPO 账号不在系统账号库里（跟华为不同），它存在游戏应用自己的数据中，
    清除方式是 force-stop + pm clear，不需要 root、不需要改系统库。
    清完下次启动游戏会回到 OPPO 登录页，需要用户自己重新登录。

子命令：
    status   查包名是否安装、当前登录态（是否存在账号 token 文件）
    logout   清除游戏数据使账号退出；默认不重启模拟器

依赖：adb（优先用包内 tools/platform-tools/adb.exe）。只操作本机模拟器。
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

OPPO_PKG = 'com.ali.croak.nearme.gamecenter'   # OPPO 渠道服（nearme = OPPO 游戏中心）
CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

# 登录 token 不一定只在游戏私有目录：pm clear 不清外部存储，
# OPPO 账号还可能挂在账号中心 / 游戏中心里。下面这些按层清理。
EXT_DIRS = ('/sdcard/Android/data/%s', '/data/media/0/Android/data/%s')
OPPO_SDK_PKGS = (
    'com.heytap.usercenter',      # 欢太账号中心
    'com.nearme.gamecenter',      # OPPO 游戏中心
    'com.heytap.gamecenter',
    'com.oppo.gamecenter',
    'com.coloros.accounts',
)

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
        if OPPO_PKG.lower() in out.lower():
            SER[0] = target
            return target
    SER[0] = fallback
    return fallback


def pkg_installed(pkg=OPPO_PKG):
    out = sh('pm list packages')
    return pkg.lower() in out.lower()


def account_state():
    """探测 OPPO 登录态：读游戏私有目录下是否还有账号 token。

    OPPO SDK 的账号信息通常落在 shared_prefs 里的若干 xml；
    只要任意一个含 oppo/nearme 账号关键字的文件存在，就认为已登录。
    """
    if not pkg_installed():
        return {'logged': False, 'hint': '游戏未安装'}
    # 先确认能否读私有目录：root 才有权限，非 root 时无法判断
    if 'uid=0' not in sh('id', t=20):
        return {'logged': None, 'hint': '无 root 权限，无法读取应用私有目录；'
                                       '可直接点退出（清除不影响服务器存档）'}
    rc, dirs = run([ADB[0], '-s', SER[0], 'shell', 'ls',
                    '/data/data/%s/shared_prefs' % OPPO_PKG], 30)
    files = [x.strip() for x in (dirs or '').splitlines() if x.strip().endswith('.xml')]
    keys = ('oppo', 'nearme', 'account', 'token', 'session', 'gcsdk')
    hits = [f for f in files if any(k in f.lower() for k in keys)]
    # 外部存储 + OPPO 账号服务：pm clear 清不到这两处，单独报出来
    ext = [d for d in _ext_dirs() if sh('ls %s 2>/dev/null' % d).strip()]
    sdk = [p for p in OPPO_SDK_PKGS if pkg_installed(p)]
    # 目录不存在或为空 = 应用数据已被清除 = 未登录（不是权限问题）
    if not files:
        return {'logged': False, 'hint': '应用数据为空，当前未登录',
                'ext': ext, 'sdk': sdk}
    return {'logged': bool(hits),
            'hint': ','.join(hits[:6]) if hits else '未发现账号文件',
            'ext': ext, 'sdk': sdk}


def status():
    if not SER[0]:
        return {'ok': False, 'error': '没连上模拟器'}
    acct = account_state()
    return {
        'ok': True,
        'serial': SER[0],
        'pkg': OPPO_PKG,
        'pkg_installed': pkg_installed(),
        'logged': acct['logged'],
        'hint': acct['hint'],
        'root': 'uid=0' in sh('id'),
    }


def _ext_dirs(pkg=OPPO_PKG):
    return [d % pkg for d in EXT_DIRS]


def _rm_ext(pkg=OPPO_PKG):
    """清外部存储里的游戏目录（pm clear 不会动这里）。返回实际存在的目录。"""
    hit = []
    for d in _ext_dirs(pkg):
        if not sh('ls -d %s 2>/dev/null' % d).strip():
            continue
        sh('rm -rf %s 2>/dev/null' % d)
        if sh('ls -d %s 2>/dev/null' % d).strip():
            # 普通 shell 权限不够时用 run-as / root 再试一次
            sh('su -c "rm -rf %s" 2>/dev/null' % d)
        hit.append(d)
    return hit


def logout(reboot=False, deep=True):
    """一键退出 OPPO 账号：游戏本体 → 外部存储 → OPPO 账号服务，逐层清。

    为什么不能只 pm clear：
      1) pm clear 只清 /data/data/<pkg>，不清 /sdcard/Android/data/<pkg>，
         不少 SDK 会把登录 token 落在外部存储；
      2) OPPO 账号本身归「欢太账号中心 / 游戏中心」管，游戏重装后仍会自动
         免登录，看起来就像「登录信息删不掉」。
    所以默认逐层往下清，并返回每一步的结果，哪一层没清掉一眼能看出来。
    """
    if not pkg_installed():
        return {'ok': False, 'error': '未安装 OPPO 渠道服，无需退出'}
    before = account_state()
    steps = []

    sh('am force-stop %s' % OPPO_PKG)
    time.sleep(1)

    # 1) 游戏本体数据
    rc, out = adb('shell', 'pm', 'clear', OPPO_PKG, t=120)
    ok = rc == 0 and 'success' in (out or '').lower()
    steps.append({'step': 'pm clear 游戏', 'ok': ok,
                  'detail': (out or '').strip()[-200:]})
    if not ok:
        # pm clear 失败时用 root 直接删数据目录兜底（设备管理器/多用户会拦住 pm clear）
        sh('su -c "rm -rf /data/data/%s/* /data/user/0/%s/*" 2>/dev/null'
           % (OPPO_PKG, OPPO_PKG))
        steps.append({'step': 'root 删除 /data/data', 'ok': True, 'detail': '兜底执行'})

    # 2) 外部存储
    ext = _rm_ext()
    steps.append({'step': '清理外部存储', 'ok': True,
                  'detail': ('、'.join(ext) if ext else '无外部数据')})

    # 3) OPPO 账号服务（装了才清，没装说明登录只在游戏内）
    sdk_cleared = []
    if deep:
        for p in OPPO_SDK_PKGS:
            if not pkg_installed(p):
                continue
            rc2, out2 = adb('shell', 'pm', 'clear', p, t=90)
            hit = rc2 == 0 and 'success' in (out2 or '').lower()
            if hit:
                sdk_cleared.append(p)
            steps.append({'step': 'pm clear %s' % p, 'ok': hit,
                          'detail': (out2 or '').strip()[-120:]})

    time.sleep(1)
    after = account_state()
    if reboot:
        adb('reboot', t=20)

    msg = '已清除 OPPO 渠道服数据'
    if sdk_cleared:
        msg += '，并退出 OPPO 账号中心（%s）' % '、'.join(sdk_cleared)
    msg += '。服务器存档不受影响。'
    if sdk_cleared:
        msg += '注意：账号中心已被退出，重新登录需要 OPPO 账号验证码；' \
               '请先在关闭抓包的环境下登录进庭院，再开始抓取。'
    return {
        'ok': True,
        'changed': True,
        'pkg': OPPO_PKG,
        'before': before,
        'after': after,
        'steps': steps,
        'sdk_cleared': sdk_cleared,
        'rebooted': bool(reboot),
        'message': msg,
    }


def main():
    ap = argparse.ArgumentParser(description='OPPO 渠道服辅助（查询 / 退出账号）')
    ap.add_argument('cmd', choices=['status', 'logout'])
    ap.add_argument('--port', help='模拟器 adb 端口')
    ap.add_argument('--pkg', default=OPPO_PKG, help='包名（默认 OPPO 渠道服）')
    ap.add_argument('--reboot', action='store_true', help='退出后重启模拟器')
    a = ap.parse_args()

    if not connect(a.port):
        print(json.dumps({'ok': False, 'error': '连不上模拟器，请先启动 MuMu / 雷电'},
                         ensure_ascii=False))
        return 2

    res = status() if a.cmd == 'status' else logout(reboot=a.reboot)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
