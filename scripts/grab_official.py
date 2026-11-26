# -*- coding: utf-8 -*-
"""一键抓官服（也支持米服、华为渠道服）存档 —— 双击 一键抓官服.bat 即可，全程无需手动操作。

做的事（按顺序全自动）：
  1. 找 adb、自动探测模拟器端口、自动识别包名（官服 com.aligames.lxqw.hhb 优先，
     米服 com.ali.croak.mi、华为渠道服 com.Aligames.lxqw.huawei 也认）
  2. 动态解析登录服务器 ali-lxqw-login.ejoy.com 的所有 A 记录（不再写死 IP）
  3. 启动 TLS 中间人代理 ws_proxy.py
  4. 装自签 CA + iptables 把登录服务器 443 重定向到代理
  5. 强制重启游戏触发全量登录，自动点「进入游戏」；可选补抓相册图层
     —— 华为渠道服走 huawei_channel：自动过隐私协议/联运声明/启动页勾选；
        若弹华为账号登录页则明确报错，等你手动登录后重跑
  6. 盯着抓包目录，等 s2c 数据稳定后停手
  7. 还原环境（撤 iptables、卸证书）、停代理
  8. 解析明文帧成协议 JSON，调用 生成实号存档.py 合成可导入存档

用法：
    双击 一键抓官服.bat
    或 python 一键抓官服.py [--port 16384] [--pkg com.aligames.lxqw.hhb] [--clear] [--tap X Y]

常用参数：
    --port 16384   指定模拟器 adb 端口（默认自动探测 16384/7555/5555/...）
    --pkg <包名>    指定包名，或写 官服/米服/华为（默认自动识别）
    --clear         清登录态强制全量（大协议更全，但可能需要重新扫码登录）
    --no-album      不补抓相册图层（默认补抓，照片才有伙伴/构图）
    --tap X Y       「进入游戏」按钮坐标（默认 450 1170，按模拟器分辨率调整）
    --timeout N     抓包总超时秒数（默认 150）

产物：
    output/一键抓官服_<时间戳>/
        ├── 协议/              解析出的 *.json（每个 cmd 一个）
        └── 旅行青蛙存档_*.json  可直接导入单机版的加密存档
"""
import argparse
import glob
import json
import os
import io
import re
import shutil
import socket
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # 绿色包根目录（自包含，不依赖 D:\frog_tools）
def _find_mitm():
    """定位 tools/mitm（代理/解析/证书）。
    绿色包脚本在 scripts/ 下，mitm 就在包根 ROOT/tools/mitm；
    从主包 05_导出助手 下运行时，回退到工具包内的兄弟绿色包目录。
    返回第一个确实含 ws_proxy.py 的目录；都缺失则返回首选路径便于报错定位。
    """
    bases = [ROOT, os.path.dirname(ROOT)]
    subs = [os.path.join('tools', 'mitm'),
            os.path.join('07_绿色包_一键抓官服', 'tools', 'mitm'),
            os.path.join('旅行青蛙_一键抓官服_绿色包_20261125', 'tools', 'mitm')]
    cands = [os.path.join(b, sub) for b in bases for sub in subs]
    for d in cands:
        if os.path.isfile(os.path.join(d, 'ws_proxy.py')):
            return d
    return cands[0]

MITM = _find_mitm()                                # 自动探测代理目录
PROXY = os.path.join(MITM, 'ws_proxy.py')
PARSE = os.path.join(MITM, 'parse_ws.py')
CAP = os.path.join(MITM, 'capture')
CA_PEM = os.path.join(MITM, 'ca.pem')
CA_NAME = '7108b1b3.0'                             # openssl x509 -subject_hash_old ca.pem
HOST_PROXY = '10.0.2.2:8443'
LOGIN_HOST = 'ali-lxqw-login.ejoy.com'
KNOWN_IPS = ['120.79.229.17', '47.106.101.196']    # 兜底：历史 A 记录
PKGS = ['com.aligames.lxqw.hhb', 'com.ali.croak.mi', 'com.Aligames.lxqw.huawei']  # 官服/米服优先
PKG_LABEL = {
    'com.aligames.lxqw.hhb': '阿里官服',
    'com.ali.croak.mi': '米服',
    'com.Aligames.lxqw.huawei': '华为渠道服',
}
PKG_ALIAS = {
    '官服': 'com.aligames.lxqw.hhb', 'aligames': 'com.aligames.lxqw.hhb',
    'official': 'com.aligames.lxqw.hhb', 'ali': 'com.aligames.lxqw.hhb',
    '米服': 'com.ali.croak.mi', 'mi': 'com.ali.croak.mi',
    '华为': 'com.Aligames.lxqw.huawei', 'huawei': 'com.Aligames.lxqw.huawei',
    'hw': 'com.Aligames.lxqw.huawei',
}
# 华为渠道服：启动流程多三道门（隐私协议 / 联运声明 / 启动页勾选），交给 huawei_channel 处理
HW_PKG = 'com.Aligames.lxqw.huawei'
PORTS = ['16384', '7555', '5555', '7556', '62001', '62026', '5554']

# 底稿（模板存档）：output 里只留这一个文件，其余才是历史抓取结果
BASE_TEMPLATE = '模板文件.json'


def base_template():
    """返回底稿路径。"""
    return os.path.join(ROOT, 'output', BASE_TEMPLATE)

ADB_CANDIDATES = [
    os.environ.get('ADB'),
    os.path.join(ROOT, 'tools', 'platform-tools', 'adb.exe'),  # 包内自带，优先
    r'D:\frog_tools\platform-tools\adb.exe',       # 以下为兼容回退，别的机器不存在也无妨
    r'D:\frog_tools\MuMu\shell\adb.exe',
    r'D:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe',
    r'C:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe',
    r'C:\LDPlayer\LDPlayer9\adb.exe',
]

CACERT_DIR = '/system/etc/security/cacerts'
TMP_DIR = '/data/local/tmp/cacerts'
SER = [None]           # 探测到的 serial
ADB = [None]
PROC = [None]          # 代理子进程
ON = [False]           # 环境是否已打开


def run(cmd, timeout=120):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        out = (p.stdout or b'').decode('utf-8', 'replace')
        err = (p.stderr or b'').decode('utf-8', 'replace')
        # 生成失败时错误常只在 stderr，合并返回以便打印真实原因
        return p.returncode, (out + ('\n' + err if err.strip() else '')).strip()
    except subprocess.TimeoutExpired:
        return 1, '超时 %ss' % timeout


def adb(*args, t=90):
    return run([ADB[0], '-s', SER[0]] + list(args), t)


def sh(cmd, t=90):
    return adb('shell', cmd, t=t)[1].strip()


def find_adb():
    for c in ADB_CANDIDATES:
        if c and os.path.isfile(c):
            return c
    found = shutil.which('adb')
    return found


def _emu_listening_ports():
    """反查模拟器进程监听的端口：MuMu 12 的 adb 端口会从 16384 漂到 16416 等，
    写死端口表在真机上经常扑空。Windows 下用 netstat + tasklist，其它平台跳过。"""
    if os.name != 'nt':
        return []
    keys = ('mumu', 'nemu', 'ldvbox', 'dnplayer', 'ldplayer', 'ld9',
            'nox', 'hd-player', 'memu', 'bluestacks', 'qemu', 'vboxheadless')
    try:
        out = subprocess.run(['netstat', '-ano', '-p', 'TCP'], capture_output=True,
                             timeout=10).stdout.decode('utf-8', 'replace')
        tl = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True,
                            timeout=10).stdout.decode('utf-8', 'replace')
    except Exception:
        return []
    names = {}
    for ln in tl.splitlines():
        c = [x.strip().strip('"') for x in ln.split('","')]
        if len(c) >= 2 and c[1].isdigit():
            names[int(c[1])] = c[0].lower()
    ports = []
    for ln in out.splitlines():
        f = ln.split()
        if len(f) < 5 or f[-2].upper() != 'LISTENING':
            continue
        port, pid = f[1].rsplit(':', 1)[-1], f[-1]
        if not (port.isdigit() and pid.isdigit()):
            continue
        if any(k in names.get(int(pid), '') for k in keys):
            p = int(port)
            if 5000 <= p <= 50000 and p != 5037 and p not in ports:
                ports.append(p)
    return ports


def _devices_online():
    rc, out = run([ADB[0], 'devices'])
    return [l.split()[0] for l in out.splitlines() if '\tdevice' in l]


def _has_game(serial, want_pkg=None):
    """这台设备是否可以执行命令、并且装了目标游戏（不装游戏的设备点了也是白点）。"""
    rc, dev = run([ADB[0], '-s', serial, 'shell', 'pm list packages'])
    if rc != 0:
        return False
    low = (dev or '').lower()
    if want_pkg:
        return want_pkg.lower() in low
    return ('aligames' in low or 'croak' in low)


def detect_serial(want_port=None, want_pkg=None):
    run([ADB[0], 'start-server'])

    # 手动指定端口：只认这一个。以前失败后还会 fallback 到「任何在线设备」，
    # 多开模拟器时会悄悄连到另一台没装华为渠道服的机器上，报「未安装」还查不出原因。
    if want_port:
        target = '127.0.0.1:%s' % want_port
        for _ in range(3):
            run([ADB[0], 'connect', target], 15)
            if _has_game(target, want_pkg):
                return target
            time.sleep(2)
        return None

    # 手动指定端口就只连它；否则先按已知端口挨个试，最后一轮才连「模拟器进程占用的端口」——
    # 多实例时全连会导致 root 状态串线、挑错机器，所以已知端口只连真正能执行命令的那个。
    # 自动探测：已知端口 -> 模拟器进程占用的端口 -> 已在线设备
    known = ['16384', '16416', '16448', '7555', '5557', '5555', '5556',
             '62001', '62025', '21503']
    for p in known:
        target = '127.0.0.1:%s' % p
        run([ADB[0], 'connect', target])
        if _has_game(target, want_pkg):
            return target
    for p in _emu_listening_ports():
        if str(p) in known:
            continue
        target = '127.0.0.1:%d' % p
        run([ADB[0], 'connect', target])
        if _has_game(target, want_pkg):
            return target
    for s in _devices_online():
        if _has_game(s, want_pkg):
            return s
    return None


def installed_pkgs():
    """列出模拟器里已安装的所有官服/米服/华为渠道服包名（去重、按 PKGS 顺序）。"""
    # 华为渠道服包名是 com.Aligames.lxqw.huawei（大写 A），不区分大小写才不会漏
    out = sh("pm list packages | grep -i -E 'aligames|croak'")
    have = set(l.strip().replace('package:', '') for l in out.splitlines() if l.strip())
    low = {p.lower(): p for p in have}
    got = [low[p.lower()] for p in PKGS if p.lower() in low]
    known = {p.lower() for p in PKGS}
    # 认不出的渠道包（只要带 aligames/croak）也列在后面，交给 --pkg 硬指定
    got += sorted(p for p in have if p.lower() not in known)
    return got


def detect_pkg(want=None):
    if want:
        w = PKG_ALIAS.get(str(want).strip().lower(), want)   # 支持 --pkg 米服 / mi / 包名
        return w
    have = installed_pkgs()
    for p in PKGS:                     # 官服优先，保持单服场景行为不变
        if p in have:
            return p
    return have[0] if have else None


def resolve_ips():
    ips = set(KNOWN_IPS)
    try:
        for fam, _, _, _, addr in socket.getaddrinfo(LOGIN_HOST, 443, socket.AF_INET):
            ips.add(addr[0])
    except Exception as e:
        print('   [!] DNS 解析 %s 失败：%s（用兜底 IP）' % (LOGIN_HOST, e))
    return sorted(ips)


def mitm_on(ips):
    """注入 CA + DNAT。"""
    sh('rm -rf %s; mkdir -p %s; cp %s/* %s/ 2>/dev/null' % (TMP_DIR, TMP_DIR, CACERT_DIR, TMP_DIR))
    rc, out = adb('push', CA_PEM, '//data//local//tmp//cacerts//' + CA_NAME)
    if rc != 0:
        print('   [X] 推送 CA 失败：%s' % out.strip())
        return False
    sh('chmod 644 %s/*; chown root:root %s/*' % (TMP_DIR, TMP_DIR))
    sh('umount %s 2>/dev/null; mount --bind %s %s' % (CACERT_DIR, TMP_DIR, CACERT_DIR))
    mounted = sh('mount | grep -c cacerts')
    for ip in ips:
        sh('iptables -t nat -D OUTPUT -p tcp -d %s --dport 443 -j DNAT --to-destination %s 2>/dev/null'
           % (ip, HOST_PROXY))
        sh('iptables -t nat -A OUTPUT -p tcp -d %s --dport 443 -j DNAT --to-destination %s'
           % (ip, HOST_PROXY))
    ON[0] = True
    print('   CA 挂载=%s，DNAT IP=%s' % ('是' if mounted.strip() not in ('0', '') else '否', ', '.join(ips)))
    return True


def mitm_off(ips):
    if not ON[0]:
        return
    for ip in ips:
        sh('iptables -t nat -D OUTPUT -p tcp -d %s --dport 443 -j DNAT --to-destination %s 2>/dev/null'
           % (ip, HOST_PROXY))
    sh('umount %s 2>/dev/null' % CACERT_DIR)
    sh('rm -rf %s' % TMP_DIR)
    ON[0] = False
    print('   已还原环境（iptables / 证书）')


def visit_museum():
    """进游戏博物馆页，触发服务器下发 museum_load（账号真实点亮的藏品）。

    实测路径（900x1600）：主界面右上「菜单」(820,65) →「小仓库」(693,60)
    →「博物馆」(240,915)。博物馆窗口一打开就会 requestMuseum()。
    坐标按当前分辨率等比缩放，适配不同模拟器。失败不影响主流程。
    """
    try:
        rc, out = adb('shell', 'wm size')
        m = re.search(r'(\d+)x(\d+)', out)
        w, h = (int(m.group(1)), int(m.group(2))) if m else (900, 1600)
        print('   博物馆入口（屏幕 %dx%d）' % (w, h))
        for x, y in ((820, 65), (693, 60), (240, 915)):
            sx, sy = int(x * w / 900), int(y * h / 1600)
            sh('input tap %d %d' % (sx, sy))
            time.sleep(1.2)
        time.sleep(3)   # 等 museum_load 下发落盘
        return True
    except Exception as e:
        print('   [!] 点博物馆失败：%s' % e)
        return False


def restart_game(pkg, clear=False, tap=(450, 1170), timeout=150):
    """强制重启游戏、自动点「进入游戏」，同时等抓包数据稳定。返回本次会话目录列表。"""
    if pkg.lower().endswith('.huawei'):
        return restart_game_huawei(pkg, timeout=timeout)

    if clear:
        print('   清登录态（可能需要重新扫码）...')
        sp = '/data/data/%s/shared_prefs/ejoysdk.xml' % pkg
        rc, xml = adb('shell', 'cat ' + sp)
        if xml.strip().startswith('<?xml'):
            import re
            for k in ('VENDOR_LOGIN', 'LOGIN_INFO', 'LAST_OFFICIAL_LOGIN_VENDOR',
                      'LAST_LOGIN', 'LAST_SET_PLAYER_INFOS'):
                xml = re.sub(r'\s*<string name="%s">.*?</string>' % re.escape(k),
                             '', xml, flags=re.S)
            tmp = os.path.join(HERE, '_ejoysdk_cleared.xml')
            with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
                f.write(xml)
            adb('push', tmp, '//data//local//tmp//ejoysdk_cleared.xml')
            sh('cp /data/local/tmp/ejoysdk_cleared.xml ' + sp)
            sh('chmod 660 ' + sp)
        else:
            print('   [!] 读不到 ejoysdk.xml，跳过清登录态')
    sh('am force-stop %s' % pkg)
    time.sleep(1)
    sh('logcat -c')
    sh('monkey -p %s -c android.intent.category.LAUNCHER 1' % pkg)

    t0 = time.time()
    last_tap = 0.0
    last_size = -1
    last_change = t0
    cur = None
    while time.time() - t0 < timeout:
        el = time.time() - t0
        if el > 10 and time.time() - last_tap > 8:
            sh('input tap %d %d' % tap)
            last_tap = time.time()
            print('   [%.0fs] 点「进入游戏」(%d,%d)' % (el, tap[0], tap[1]))
        sessions = [d for d in glob.glob(os.path.join(CAP, '*'))
                    if os.path.isdir(d) and os.path.getmtime(d) >= t0 - 3]
        size, big = 0, None
        for d in sessions:
            p = os.path.join(d, 's2c.bin')
            if os.path.isfile(p):
                s = os.path.getsize(p)
                if s > size:
                    size, big = s, d
        if size > 0 and size != last_size:
            last_size, last_change, cur = size, time.time(), big
            print('   [%.0fs] 已抓 %d 字节（%s）' % (el, size, os.path.basename(big or '')))
        elif size > 0 and time.time() - last_change >= 8:
            print('   抓包数据已稳定')
            return sessions
        time.sleep(2)
    return [d for d in glob.glob(os.path.join(CAP, '*'))
            if os.path.isdir(d) and os.path.getmtime(d) >= t0 - 3]


def _drain_capture(t0, max_wait=90):
    """等本次会话的 s2c.bin 不再增长（最多 max_wait 秒），返回会话目录列表。"""
    last_size, last_change = -1, time.time()
    end = time.time() + max_wait
    while time.time() < end:
        sessions = [d for d in glob.glob(os.path.join(CAP, '*'))
                    if os.path.isdir(d) and os.path.getmtime(d) >= t0 - 3]
        size, big = 0, None
        for d in sessions:
            p = os.path.join(d, 's2c.bin')
            if os.path.isfile(p):
                s = os.path.getsize(p)
                if s > size:
                    size, big = s, d
        if size > 0 and size != last_size:
            last_size, last_change = size, time.time()
            print('   已抓 %d 字节（%s）' % (size, os.path.basename(big or '')))
        elif size > 0 and time.time() - last_change >= 8:
            print('   抓包数据已稳定')
            break
        time.sleep(2)
    return [d for d in glob.glob(os.path.join(CAP, '*'))
            if os.path.isdir(d) and os.path.getmtime(d) >= t0 - 3]


def _hw_precheck(pkg):
    """华为渠道服抓取前置体检：HMS Core / 登录状态 / CPU ABI。只提示，不阻断。"""
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import huawei_channel as hw
        hw.ADB[0] = ADB[0]
        hw.SER[0] = SER[0]
        st = hw.status()
    except Exception as e:
        print('   [!] 华为渠道服体检失败：%s' % e)
        return
    if not st.get('hms_installed'):
        print('   [!] 未安装 HMS Core，华为渠道服会卡在「安装 HMS」死循环。')
        print('       解决：把 hmscore.apk 放到 tools/apk/，或点界面上的「准备华为渠道服」。')
    if not st.get('account'):
        print('   [!] 当前没有已登录的华为账号，稍后需要你在模拟器里手动输入账号密码。')
    abi = st.get('abi') or ''
    if abi and 'arm' not in abi.lower():
        print('   [!] 当前 ABI=%s：阿里加固的 x86 库在模拟器上会 SIGSEGV，建议改 armeabi-v7a 重装。' % abi)
    if st.get('account'):
        print('   华为账号：%s（HMS=%s，ABI=%s）' % (
            st['account'], '有' if st.get('hms_installed') else '无', abi or '未知'))


def restart_game_huawei(pkg, timeout=150):
    """华为渠道服专用启动：交给 huawei_channel 处理隐私协议 / 联运声明 / 启动页勾选。

    华为渠道服比官服多三道门，盲点「进入游戏」只会撞在协议页上：
      1. 首次隐私协议（原生弹窗）
      2. 华为联运服务声明（HMS 弹窗）
      3. 启动页底部「我已阅读并同意」复选框（自绘 UI，必须先勾再点进入）
    """
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import huawei_channel as hw
    except Exception as e:
        print('   [X] 加载 huawei_channel 失败：%s' % e)
        return []

    hw.ADB[0] = ADB[0]
    hw.SER[0] = SER[0]
    print('   华为渠道服启动流程（自动过协议页；若弹登录页需人工输入）...')
    t0 = time.time()
    res = hw.launch(pkg=pkg, timeout=timeout)
    state = res.get('state') or 'unknown'
    print('   启动结果：%s' % state)
    if not res.get('ok'):
        msg = res.get('message') or res.get('error') or '未知原因'
        print('   [X] %s' % msg)
        if state == 'need_login':
            print('   [!] 请在模拟器窗口里手动输入华为账号密码，登录成功后再点一次「开始抓取」。')
            print('       想换号可先用界面上的「一键退出华为账号」，再重新登录。')
        elif state == 'need_hms':
            print('   [!] 缺 HMS Core：把 hmscore.apk 放到 tools/apk/ 后重试，或点「准备环境」按钮。')
        elif state == 'register_closed':
            print('   [!] 该华为账号在本渠道没有历史角色，且服务器已停止新用户注册，换号才能进。')
        elif state == 'stuck':
            print('   [!] 游戏停在非预期页面，已截图到 output/ 便于排查。')
        return []

    return _drain_capture(t0, max_wait=min(120, max(30, timeout // 2)))


def size_of_s2c(d):
    p = os.path.join(d, 's2c.bin')
    return os.path.getsize(p) if os.path.isfile(p) else 0



def merge_injected_logs(sessions, proto_dir):
    """把代理主动补抓的日志并进协议目录（s2c.bin 若没解析到，这里兜底）。

    ws_proxy.py 的 Injector 会把：
      album_by_id.jsonl      -> {'cmd':'album.load_by_id_list','pics':[...]}
      moment_by_inject.jsonl -> {'cmd':'misc.moment_load','data':{...}}
    落盘到抓包会话目录。生成存档脚本要的是 {'cmd':..,'data':{..}}，这里补一层。
    """
    added = []
    for d in sessions:
        # 相册图层：多次响应的 pic_list 合并去重
        src = os.path.join(d, 'album_by_id.jsonl')
        if os.path.isfile(src):
            pics, seen = [], set()
            for line in io.open(src, encoding='utf-8', errors='ignore'):
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                for p in (o.get('pics') or []):
                    if not isinstance(p, dict) or p.get('id') is None:
                        continue
                    if p['id'] in seen:
                        continue
                    seen.add(p['id'])
                    pics.append(p)
            if pics:
                dst = os.path.join(proto_dir, 'album.load_by_id_list.json')
                old = []
                if os.path.isfile(dst):
                    try:
                        old = (json.load(open(dst, encoding='utf-8')).get('data') or {}).get('pic_list') or []
                    except Exception:
                        old = []
                have = {p.get('id') for p in old if isinstance(p, dict)}
                for p in pics:
                    if p['id'] not in have:
                        old.append(p)
                json.dump({'cmd': 'album.load_by_id_list', 'data': {'pic_list': old}},
                          open(dst, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
                added.append('相册图层 %d 张' % len(old))
        # 美好瞬间：取 list 最长的一次
        src = os.path.join(d, 'moment_by_inject.jsonl')
        if os.path.isfile(src):
            best = None
            for line in io.open(src, encoding='utf-8', errors='ignore'):
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                data = o.get('data') or {}
                if best is None or len(data.get('list') or []) > len((best.get('data') or {}).get('list') or []):
                    best = {'cmd': 'misc.moment_load', 'data': data}
            if best is not None and (best.get('data') or {}).get('list'):
                dst = os.path.join(proto_dir, 'misc.moment_load.json')
                json.dump(best, open(dst, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
                added.append('美好瞬间 %d 个' % len(best['data']['list']))
    return added

def parse_sessions(sessions, proto_dir):
    os.makedirs(proto_dir, exist_ok=True)
    n = 0
    for d in sorted(sessions, key=size_of_s2c):     # 大的后解析，覆盖小的
        if size_of_s2c(d) < 1000:
            continue
        tmp = os.path.join(d, '_proto')
        rc, out = run([sys.executable, PARSE, d, tmp], timeout=60)
        if rc != 0:
            print('   [!] 解析 %s 失败：%s' % (os.path.basename(d), out.strip()[:200]))
            continue
        for f in glob.glob(os.path.join(tmp, '*.json')):
            dst = os.path.join(proto_dir, os.path.basename(f))
            if not os.path.exists(dst) or os.path.getsize(f) > os.path.getsize(dst):
                shutil.copy2(f, dst)
                n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description='一键抓官服/米服存档')
    ap.add_argument('--port', help='模拟器 adb 端口')
    ap.add_argument('--pkg', help='包名，或 官服/米服/华为（默认自动识别）')
    ap.add_argument('--clear', action='store_true', help='清登录态强制全量（可能要重新登录）')
    ap.add_argument('--no-album', action='store_true', help='不补抓相册图层')
    ap.add_argument('--no-museum', action='store_true', help='不自动进博物馆抓真实藏品（默认会抓）')
    ap.add_argument('--tap', nargs=2, type=int, default=[450, 1170], metavar=('X', 'Y'))
    ap.add_argument('--timeout', type=int, default=150, help='抓包总超时秒数')
    a = ap.parse_args()

    print('=' * 68)
    print('  旅行青蛙 一键抓官服存档')
    print('=' * 68)

    ADB[0] = find_adb()
    if not ADB[0]:
        print('[X] 找不到 adb。用 --adb 或把 D:\\frog_tools\\platform-tools\\adb.exe 放好。')
        return 1
    print('adb: %s' % ADB[0])

    want = PKG_ALIAS.get(str(a.pkg).strip().lower(), a.pkg) if a.pkg else None
    SER[0] = detect_serial(a.port, want)
    if not SER[0]:
        if a.port:
            print('[X] 端口 %s 上没有可用的游戏设备（连不上，或那台没装目标服）。\n'
                  '    多开模拟器时请确认端口对应的是装了该服的实例。' % a.port)
        else:
            print('[X] 连不上模拟器。先打开 MuMu/雷电并启动游戏，或用 --port 指定端口。')
        return 1
    print('设备: %s' % SER[0])

    ident = sh('id')
    if 'uid=0' not in ident:
        print('   shell 不是 root，尝试 adb root 提权...')
        run([ADB[0], '-s', SER[0], 'root'], timeout=30)
        time.sleep(3)
        # 注意：提权后不要 disconnect/重连，那会把 root 上下文丢掉。
        # adb root 重启 adbd 后会短暂 offline，等它自己起来即可。
        for _ in range(5):
            ident = sh('id')
            if 'uid=0' in ident:
                break
            time.sleep(1)
        if 'uid=0' not in ident:
            print('[X] adb root 后仍不是 root（%s）。请在模拟器设置里打开 root/ADB 调试。' % ident[:80])
            return 1
        print('   已提权为 root')

    # 决定要抓哪些服：--pkg 指定单个；否则把检测到的所有官方服都抓一遍
    if a.pkg:
        packages = [detect_pkg(a.pkg)]
    else:
        packages = installed_pkgs()
    if not packages or not packages[0]:
        print('[X] 没找到官服/米服包名。确认游戏已安装，或用 --pkg 指定。')
        return 1
    packages = [p for p in packages if p]

    if len(packages) > 1:
        # 两个服都在：用独立子进程逐个抓，每个进程自己装/还原证书和 DNAT，互不污染。
        # 单进程里连抓两个服会让代理子进程、iptables、adb 状态互相踩，风险高。
        print('   检测到 %d 个官方服，将依次抓取：' % len(packages))
        for p in packages:
            print('     - %s（%s）' % (p, PKG_LABEL.get(p, '未知渠道')))
        rc_all = 0
        for i, p in enumerate(packages):
            print('\n' + '#' * 68)
            print('#  [%d/%d] 开始抓 %s（%s）' %
                  (i + 1, len(packages), PKG_LABEL.get(p, p), p))
            print('#' * 68)
            sub = [sys.executable, os.path.abspath(__file__), '--pkg', p]
            if a.port:
                sub += ['--port', a.port]
            if a.clear:
                sub += ['--clear']
            if a.no_album:
                sub += ['--no-album']
            if a.no_museum:
                sub += ['--no-museum']
            sub += ['--tap', str(a.tap[0]), str(a.tap[1]), '--timeout', str(a.timeout)]
            rc = subprocess.call(sub)
            if rc != 0:
                rc_all = rc
                print('   [!] %s 抓取失败，继续下一个服' % PKG_LABEL.get(p, p))
            if i < len(packages) - 1:
                time.sleep(3)   # 给 adb / 证书环境一点恢复时间
        print('\n全部 %d 个服处理完毕%s。' % (len(packages), '（有失败，见上）' if rc_all else ''))
        return rc_all

    pkg = packages[0]
    print('包名: %s（%s）' % (pkg, PKG_LABEL.get(pkg, '未知渠道')))
    if pkg.lower().endswith('.huawei'):
        _hw_precheck(pkg)
    if pkg.lower().endswith('.huawei'):
        _hw_precheck(pkg)

    ips = resolve_ips()
    print('登录服务器: %s -> %s' % (LOGIN_HOST, ', '.join(ips)))

    out_dir = os.path.join(ROOT, 'output', '一键抓官服_%s' % time.strftime('%Y%m%d_%H%M%S'))
    proto_dir = os.path.join(out_dir, '协议')
    os.makedirs(out_dir, exist_ok=True)

    try:
        print('\n[1/5] 启动代理...')
        proxy_cmd = [sys.executable, PROXY, '--listen', '8443', '--host', LOGIN_HOST]
        if not a.no_album:
            proxy_cmd.append('--inject-album')
        plog = open(os.path.join(out_dir, '代理日志.txt'), 'w', encoding='utf-8')
        PROC[0] = subprocess.Popen(proxy_cmd, stdout=plog, stderr=subprocess.STDOUT)
        time.sleep(2)
        if PROC[0].poll() is not None:
            plog.flush()
            with open(os.path.join(out_dir, '代理日志.txt'), 'r', encoding='utf-8', errors='replace') as f:
                tail = f.read()[-300:]
            print('[X] 代理启动失败（端口 8443 被占用？）：%s' % tail)
            return 1
        print('   代理已启动，补抓相册=%s' % ('关' if a.no_album else '开'))

        print('\n[2/5] 装证书 + 重定向登录流量...')
        if not mitm_on(ips):
            return 1

        print('\n[3/5] 重启游戏触发登录（会自动点「进入游戏」）...')
        sessions = restart_game(pkg, clear=a.clear, tap=tuple(a.tap), timeout=a.timeout)
        print('   本次会话 %d 个' % len(sessions))

        if not a.no_museum:
            # 独立步骤：游戏已进主界面、代理还在，此时进博物馆触发 museum_load。
            # 不在 restart_game 循环里做，避免被定时「进入游戏」点击打断。
            print('\n   [3.5/5] 进博物馆采真实藏品（触发 museum_load）...')
            size_before = sum(size_of_s2c(d) for d in sessions)
            if visit_museum():
                # 等 s2c 增长（museum_load 下发）或最多 20 秒
                for _ in range(10):
                    time.sleep(2)
                    if sum(size_of_s2c(d) for d in sessions) > size_before:
                        print('   已收到博物馆协议数据')
                        time.sleep(2)
                        break
                else:
                    print('   [!] 未检测到 museum_load 下发，博物馆藏品将用兜底')

        print('\n[4/5] 还原环境 + 停代理...')
        sh('am force-stop %s' % pkg)
        mitm_off(ips)
        try:
            PROC[0].terminate()
            PROC[0].wait(timeout=5)
        except Exception:
            try:
                PROC[0].kill()
            except Exception:
                pass
        PROC[0] = None

        print('\n[5/5] 解析协议 + 生成存档...')
        n = parse_sessions(sessions, proto_dir)
        try:
            _merged = merge_injected_logs(sessions, proto_dir)
            if _merged:
                print('   代理补抓已并入协议：%s' % '、'.join(_merged))
        except Exception as _e:
            print('   [!] 代理补抓日志合并失败：%s' % _e)
        names = sorted(os.path.basename(f)[:-5] for f in glob.glob(os.path.join(proto_dir, '*.json')))
        print('   解析出 %d 个协议文件，共 %d 个 cmd' % (n, len(names)))
        key = ['client.load_role', 'item.load_items', 'album.load_all',
               'travel.load_note', 'furniture.load_furniture']
        miss = [k for k in key if k not in names]
        if miss:
            print('   [!] 缺关键协议：%s' % ', '.join(miss))
            print('       大协议有时不在缓存登录态里下发。可加 --clear 重跑（可能要重新登录）。')
        if 'misc.moment_load' not in names:
            print('   [i] 未抓到美好瞬间协议（misc.moment_load）：本次不写入该字段。')
            print('       该项在完整登录时下发；如需补抓可加 --clear 重跑，或用编辑器的「美好瞬间」页手动勾选。')

        # 存档文件名 = 昵称 + 旅行青蛙存档_官服抓取_ + 当前时间
        # 昵称从刚解析出的 client.load_role.json 里取 frog.name，读不到就用「未知」
        nick = ''
        try:
            role_file = os.path.join(proto_dir, 'client.load_role.json')
            if os.path.isfile(role_file):
                role = json.load(open(role_file, encoding='utf-8'))
                nick = str((role.get('data') or {}).get('frog', {}).get('name') or '').strip()
        except Exception:
            pass
        nick = re.sub(r'[\\/:*?"<>|\r\n\t]', '', nick)[:20] or '未知昵称'
        stamp = time.strftime('%Y%m%d_%H%M%S')
        base = base_template()
        out_json = os.path.join(out_dir, '%s旅行青蛙存档_官服抓取_%s.json' % (nick, stamp))
        print('   存档命名：%s' % os.path.basename(out_json))
        rc, out = run([sys.executable, os.path.join(HERE, '生成实号存档.py'),
                       '--proto', proto_dir, '--base', base, '--out', out_json,
                       '--with-album', '--with-moment', '--real-fur', '--museum-from-proto',
                       '--museum-collections'], timeout=120)
        if rc != 0 or not os.path.isfile(out_json):
            print('   [X] 生成存档失败：%s' % out.strip()[:500])
            return 1

        # 顺手留一份解密明文（原始数据），方便排查/二次编辑。明文没有 format 信封，不能导入游戏。
        try:
            sys.path.insert(0, os.path.join(ROOT, 'lib'))
            import tf as _tf
            _raw, _env = _tf.load(out_json)
            raw_json = os.path.join(out_dir, os.path.basename(out_json).replace('.json', '') + '_原始数据_明文不可导入.json')
            json.dump(_raw, open(raw_json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        except Exception as _e:
            raw_json = None
            print('   [!] 原始数据备份失败：%s' % _e)

        print('\n' + '=' * 68)
        print('  完成！可导入存档：')
        print('  %s' % out_json)
        if raw_json:
            print('  原始数据(明文备份)：%s' % raw_json)
        print('  协议目录：%s' % proto_dir)
        print('  提示：抓完请手动重开一次游戏恢复联网。')
        print('=' * 68)
        return 0
    finally:
        # 任何异常都还原环境，避免模拟器断网
        if PROC[0] is not None:
            try:
                PROC[0].terminate()
                PROC[0].wait(timeout=5)
            except Exception:
                pass
        if ON[0]:
            mitm_off(ips if 'ips' in dir() else KNOWN_IPS)


if __name__ == '__main__':
    raise SystemExit(main())
