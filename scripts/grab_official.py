# -*- coding: utf-8 -*-
"""一键抓官服（也支持米服、华为渠道服）存档 —— 双击 一键抓官服.bat 即可，全程无需手动操作。

做的事（按顺序全自动）：
  1. 找 adb、自动探测模拟器端口、自动识别包名（官服 com.aligames.lxqw.hhb 优先，
     米服 com.ali.croak.mi、华为渠道服 com.Aligames.lxqw.huawei、b站服 com.ali.croak.bilibili、
     OPPO 渠道服 com.ali.croak.nearme.gamecenter、vivo 渠道服 com.ali.croak.vivo 也认）
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
    --pkg <包名>    指定包名，或写 官服/米服/华为/b站（默认自动识别）
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
import hashlib
import json
import os
import io
import re
import shutil
import socket
import subprocess
import sys
import time

# 老系统/老 Python 可能没有 reconfigure（3.7+ 才有），缺了也不能让脚本在 import 阶段就崩
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    import grab_diag as diag          # 抓不到流量时的自检/自愈（可选，缺了也能照常跑）
except Exception:
    diag = None
try:
    import game_display as disp       # 游戏可能在虚拟 display（MuMu KeepAlive）
except Exception:
    disp = None
try:
    import cert_install               # 系统 CA 必须挂进 zygote/应用 mount namespace
except Exception:
    cert_install = None
try:
    import game_boot as boot          # 启动页：勾协议 / 点同意弹窗 / 认登录页
except Exception:
    boot = None
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
PKGS = ['com.aligames.lxqw.hhb', 'com.ali.croak.mi',
        'com.Aligames.lxqw.huawei', 'com.ali.croak.bilibili',
        'com.ali.croak.nearme.gamecenter',   # OPPO 渠道服（nearme = OPPO 游戏中心）
        'com.ali.croak.vivo']                # vivo 渠道服（官方 2025-04 已停运，保留识别）
PKG_LABEL = {
    'com.aligames.lxqw.hhb': '阿里官服',
    'com.ali.croak.mi': '米服',
    'com.Aligames.lxqw.huawei': '华为渠道服',
    'com.ali.croak.bilibili': 'b站服',
    'com.ali.croak.nearme.gamecenter': 'OPPO渠道服',
    'com.ali.croak.vivo': 'vivo渠道服',
}
PKG_ALIAS = {
    '官服': 'com.aligames.lxqw.hhb', 'aligames': 'com.aligames.lxqw.hhb',
    'official': 'com.aligames.lxqw.hhb', 'ali': 'com.aligames.lxqw.hhb',
    '米服': 'com.ali.croak.mi', 'mi': 'com.ali.croak.mi',
    '华为': 'com.Aligames.lxqw.huawei', 'huawei': 'com.Aligames.lxqw.huawei',
    'hw': 'com.Aligames.lxqw.huawei',
    # b 站服（哔哩哔哩渠道）：登录域名未知，抓包走 host-all 兜底
    'b站': 'com.ali.croak.bilibili', 'b服': 'com.ali.croak.bilibili',
    'bilibili': 'com.ali.croak.bilibili', 'bili': 'com.ali.croak.bilibili',
    # OPPO 渠道服（nearme = OPPO 游戏中心）/ vivo 渠道服
    'oppo': 'com.ali.croak.nearme.gamecenter', 'o服': 'com.ali.croak.nearme.gamecenter',
    'nearme': 'com.ali.croak.nearme.gamecenter',
    'vivo': 'com.ali.croak.vivo', 'v服': 'com.ali.croak.vivo',
}
# 全渠道统一走 host-all 解密：
# 早期只给登录域名不固定的渠道服开，结果官服反被"按 SNI 过滤"坑惨——
# 阿里云 ALB 的登录域名会变、客户端还可能 IP 直连导致 SNI 为空，
# 代理只认 LOGIN_HOST 时会把真正的游戏流量全透传（连接进来但 0 次 TLS 建立）。
# 解析脚本只认协议 cmd，多解密的 MuMu 遥测/账号 SDK 流量会被自动忽略，无副作用。
HOST_ALL_PKGS = set(PKGS)
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
    """列出模拟器里已装的官服/米服/华为/OPPO/vivo/b站渠道服包名（去重、按 PKGS 顺序）。"""
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


def verify_cert_pair():
    """用 Python ssl 在本地做一次真实 TLS 握手，验证 ca.pem 能验过 srv.pem。

    v1.4 重签过 CA（换了密钥但保留 subject，文件名 hash 仍是 7108b1b3.0）。
    如果升级时只覆盖了 srv.pem 没覆盖 ca.pem（或反之），文件名和 subject 都
    看不出异常，但客户端会全部握手失败——表现就是「连接进来几千条、解密 0 条」。
    这里直接做握手自检，不配套就大声报错，不再静默抓空。
    返回 (ok, msg)，纯标准库实现，不依赖 openssl 命令行。
    """
    import ssl, socket, threading, hashlib
    ca = os.path.join(MITM, 'ca.pem')
    srvp = os.path.join(MITM, 'srv.pem')
    srvk = os.path.join(MITM, 'srv.key')
    try:
        for p in (ca, srvp, srvk):
            if not os.path.isfile(p):
                return False, '缺少证书文件 %s' % os.path.basename(p)
        sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        sctx.load_cert_chain(srvp, srvk)
        cctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        cctx.check_hostname = False
        cctx.verify_mode = ssl.CERT_REQUIRED
        cctx.load_verify_locations(cafile=ca)
    except Exception as e:
        return False, '证书文件加载失败：%s' % e

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        port = listener.getsockname()[1]
    except Exception as e:
        return True, '（跳过握手自检：无法开本地端口 %s）' % e

    def _server():
        try:
            conn, _ = listener.accept()
            with sctx.wrap_socket(conn, server_side=True) as t:
                t.recv(1)
        except Exception:
            pass
        finally:
            try:
                listener.close()
            except Exception:
                pass

    threading.Thread(target=_server, daemon=True).start()
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=5) as raw:
            with cctx.wrap_socket(raw, server_hostname='localhost') as t:
                t.send(b'x')
        fp = hashlib.md5(open(ca, 'rb').read()).hexdigest()[:8]
        return True, 'ca.pem ↔ srv.pem 配套（CA 指纹 %s）' % fp
    except Exception as e:
        return False, ('ca.pem 验不过 srv.pem —— 证书新旧混装！'
                       '请完整解压安装包，确保 ca.pem / srv.pem / srv.key 三个一起覆盖。'
                       '（%s）' % e)


def free_proxy_port():
    """启动代理前清掉占用 8443 的残留代理进程。

    抓取任务被强杀/程序直接关掉时，ws_proxy 子进程会变成孤儿一直监听 8443。
    新代理绑不上端口，游戏流量就进了那个**旧代码**的代理——旧版没有账号域名
    透传、长连接还会被 15 秒超时掐断，于是玩家看到「开着抓取器进不去游戏」。
    """
    if os.name != 'nt':
        return []
    port = HOST_PROXY.rsplit(':', 1)[-1]
    flags = 0x08000000
    try:
        r = subprocess.run(['netstat', '-ano'], capture_output=True,
                           timeout=20, creationflags=flags)
        # netstat 在中文 Windows 上是 GBK，不能按 UTF-8 解
        out = (r.stdout or b'').decode('gbk', 'ignore')
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or 'LISTEN' not in line.upper():
            continue
        if parts[1].rsplit(':', 1)[-1] != port:
            continue
        pid = parts[-1]
        if pid.isdigit() and int(pid) > 4 and int(pid) not in pids:
            pids.append(int(pid))
    killed = []
    for pid in pids:
        try:
            r = subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                               capture_output=True, timeout=10, creationflags=flags)
            if r.returncode == 0:
                killed.append(pid)
        except Exception:
            pass
    if killed:
        time.sleep(1)
    return killed


def proxy_port_busy():
    """8443 是否仍被占用（残留代理没清掉时用来说明原因）。"""
    port = int(HOST_PROXY.rsplit(':', 1)[-1])
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('0.0.0.0', port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def mitm_on(ips):
    """注入 CA + DNAT。

    关键：不能只在 adb shell 里 mount --bind /system/etc/security/cacerts。
    Android 10+ 的每个应用都有独立 mount namespace（实测 shell 4026531840、
    zygote64 4026532201、应用 4026532208），shell 侧挂载对游戏不可见，
    表现就是客户端 SSLV3_ALERT_CERTIFICATE_UNKNOWN、抓不到任何游戏流量。
    现在由 cert_install 同时挂到 shell + zygote/zygote64，并按 CA 的 MD5 校验。
    """
    if cert_install is None:
        print('   [X] 缺少 scripts/cert_install.py，无法把证书装进游戏可见的命名空间。')
        print('       请完整解压安装包，不要只替换部分脚本。')
        return False
    ok, src, n, detail = cert_install.prepare(sh, TMP_DIR)
    print('   证书目录：%s' % detail)
    if not ok:
        print('   [X] 准备系统根证书失败：%s' % detail)
        return False
    rc, out = adb('push', CA_PEM, '//data//local//tmp//cacerts//' + CA_NAME)
    if rc != 0:
        print('   [X] 推送 CA 失败：%s' % out.strip())
        return False
    sh('chmod 644 %s/*; chown root:root %s/*' % (TMP_DIR, TMP_DIR))
    sh('chcon -R u:object_r:system_security_cacerts_file:s0 %s 2>/dev/null' % TMP_DIR)
    ca_md5 = hashlib.md5(open(CA_PEM, 'rb').read()).hexdigest()
    res = cert_install.mount(sh, TMP_DIR, CA_NAME, ca_md5)
    if not res.get('ok'):
        print('   [X] 证书没能装进游戏可见的命名空间：%s' % res.get('detail'))
        print('       Android 10+ 每个应用有独立 mount namespace；只在 adb shell 里挂载，')
        print('       游戏进程看不到（会报 CERTIFICATE_UNKNOWN）。请确认模拟器已 root、')
        print('       系统里有 zygote/zygote64 且 nsenter 可用后重试。')
        return False
    print('   CA 挂载=是（%s，系统证书 %d 张）' % (
        res.get('mode') or '命名空间', res.get('cert_count') or (n + 1)))
    for ip in ips:
        sh('iptables -t nat -D OUTPUT -p tcp -d %s --dport 443 -j DNAT --to-destination %s 2>/dev/null'
           % (ip, HOST_PROXY))
        sh('iptables -t nat -A OUTPUT -p tcp -d %s --dport 443 -j DNAT --to-destination %s'
           % (ip, HOST_PROXY))
    ON[0] = True
    print('   DNAT IP=%s' % ', '.join(ips))
    # 顺手验一下「模拟器 -> 宿主 8443」这一跳：换电脑最常见的坑就是这里被防火墙掐断，
    # 而且表现极其迷惑——游戏照常能玩，代理却一条连接都收不到。
    if diag:
        try:
            ok, why = diag.probe(sh, HOST_PROXY)
            print('   宿主代理连通性：%s' % ('正常' if ok else '不通（%s）' % why))
            if ok:
                # 登录服是阿里云 ALB，同一域名会在几十个 IP 之间轮换，实测同一台机器
                # 上一轮连 47.106.101.196、下一轮连 49.4.37.205——只按 DNS 解析出的 IP
                # 劫持必然时不时整轮抓空。所以宿主端口确认通之后再加一条全量兜底：
                # 代理是按 SNI 分流的，非登录服域名纯透传，不影响其它联网。
                good, err = diag.add_all_rule(sh, HOST_PROXY)
                if good:
                    print('   已开启全量 443 兜底劫持（不依赖 IP 名单）')
                else:
                    print('   [!] 全量兜底劫持没加上：%s（仍按 IP 名单抓）' % err)
            else:
                print('   [!] 模拟器连不到宿主 8443，这一轮很可能抓不到任何数据。')
                print('       放行方法：Windows 安全中心 -> 防火墙 -> 允许应用通过防火墙，')
                print('       把 python.exe 的「专用」「公用」都勾上（首次弹窗点了取消会一直静默拦截）。')
        except Exception:
            pass
    return True


def mitm_off(ips):
    """还原抓包环境。无条件执行、幂等——即使异常退出 ON 没置位也要清残留。

    米服卡启动页的根因就在这：旧版只删按 IP 的规则、不删全量兜底，且依赖
    diag.ALL_ON（子进程加规则、父进程标志仍是 False），结果「提示已还原」其实
    网络还被劫持、根证书目录还被替换 → 小米 FlySDK 校验证书链失败卡死。
    """
    res = None
    if diag:
        try:
            res = diag.cleanup_all(sh, HOST_PROXY, CACERT_DIR, TMP_DIR)
        except Exception as e:
            print('   [!] 自动清理异常：%s' % e)
    if res is None:
        # 兜底：diag 不可用时至少删掉按 IP 的规则 + 全量兜底
        for ip in ips:
            sh('iptables -t nat -D OUTPUT -p tcp -d %s --dport 443 -j DNAT '
               '--to-destination %s 2>/dev/null' % (ip, HOST_PROXY))
        sh('iptables -t nat -D OUTPUT -p tcp --dport 443 -j DNAT '
           '--to-destination %s 2>/dev/null' % HOST_PROXY)
        sh('umount %s 2>/dev/null' % CACERT_DIR)
        sh('rm -rf %s' % TMP_DIR)
    ON[0] = False
    if res and not res.get('cleaned'):
        print('   [X] 抓包环境未完全还原：残留劫持规则=%s 条，证书挂载=%s' % (
            res.get('rules_after'), '仍在' if res.get('mounted_after') else '已卸'))
        print('       游戏可能因此卡启动页。请在网页点「恢复联网」，或手动重启模拟器。')
    else:
        print('   已还原环境（iptables / 证书，已校验无残留）')


# ---------------- 博物馆（账号真实点亮的藏品）----------------
# 导航逻辑全部在 scripts/museum.py：
#   进「菜单(790,65) → 小仓库(700,65) → 博物馆」触发 museum_load；
#   博物馆那格是截屏按颜色认「红牌坊」图标定位的（各渠道服格子数不同，不能写死坐标）。
# 这里只留薄代理 —— grab_official.py 被整文件覆盖过两次，逻辑放模块里才不会跟着丢，
# _apply_hw_patch.py 也只需要把旧函数换成下面这几行。
def visit_museum(pkg=None, retries=3):
    """进博物馆页触发服务器下发 museum_load（实现见 scripts/museum.py）。"""
    try:
        import museum
        museum.setup(ADB[0], SER[0], pkg)   # 必须带包名，否则定位不到虚拟显示
        return museum.visit(pkg, retries)
    except Exception as e:
        print('   [!] 博物馆模块加载失败：%s' % e)
        return False


def museum_proto_seen(sessions):
    """s2c 里是否已出现 museum_list 数组（实现见 scripts/museum.py）。"""
    try:
        import museum
        return museum.proto_seen(sessions)
    except Exception:
        return False


def _sessions_since(since):
    """抓包目录里 s2c.bin 在 since 之后仍在写的会话（实现见 scripts/museum.py）。"""
    try:
        import museum
        return museum.sessions_since(CAP, since)
    except Exception:
        return []


def _tap_game(x, y, pkg=None):
    """在【游戏所在显示】上点一次（坐标是 900x1600 基准，内部按分辨率缩放）。

    MuMu KeepAlive 下默认屏点不到游戏；非 900x1600 的模拟器上不缩放会点偏。
    """
    px, py = int(x), int(y)
    if boot:
        try:
            px, py = boot.to_phys(x, y)
        except Exception:
            pass
    if disp:
        disp.setup(ADB[0], SER[0], pkg)
        if disp.tap_base(px, py):
            return True
    rc, _ = adb('shell', 'input tap %d %d' % (px, py))
    return rc == 0


def auth_stuck_hint(log_path):
    """判断「卡在渠道账号认证」并返回人话提示（OPPO 实测）。

    判据：代理日志里 heytap/oppomobile/nearme 这些渠道账号域名连了很多次，
    而登录服 account.flysdk.cn 关连接时 c2s 一直是 0 字节 —— 说明游戏连上了
    账号 SDK，但认证请求根本没发出去，游戏会一直停在登录/加载页。
    这不是证书问题（证书有问题会 CERTIFICATE_UNKNOWN），也不该继续等超时。
    """
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    except Exception:
        return ''
    if not text:
        return ''
    heytap = len(re.findall(r'\[MITM\] [\w.\-]*(?:heytap|oppomobile|nearme)[\w.\-]*', text))
    zero = 0
    for m in re.finditer(r'account\.flysdk\.cn 连接关闭，c2s=(\d+)B', text):
        if int(m.group(1)) == 0:
            zero += 1
    if heytap >= 20 and zero >= 1:
        return ('代理日志里 OPPO 账号域名（heytap / oppomobile）连了 %d 次，而登录服 '
                'account.flysdk.cn 一直是 0 字节 —— 游戏卡在 OPPO 账号认证，不是证书问题。'
                '请点「一键恢复联网」，在模拟器里手动登录并进到庭院，再开始抓取。' % heytap)
    return ''


def _game_ca_visible(pkg):
    """游戏进程里能否读到本包 CA（经 /proc/<pid>/root 验证命名空间继承）。

    返回 True/False；进程还没起来返回 None。只用于日志预警：
    Android 10+ 若只在 shell 里挂证书，这里会是 False，随后必然 CERTIFICATE_UNKNOWN。
    """
    if cert_install is None:
        return None
    pids = [x for x in sh('pidof %s' % pkg).split() if x.isdigit()]
    if not pids:
        return None
    try:
        md5 = hashlib.md5(open(CA_PEM, 'rb').read()).hexdigest()
    except Exception:
        md5 = ''
    dirs = cert_install.ca_dirs(sh)
    for pid in pids:
        for d in dirs:
            got = cert_install.file_md5(
                sh, None, '/proc/%s/root%s/%s' % (pid, d, CA_NAME))
            if got and (not md5 or got == md5):
                return True
    return False


def restart_game(pkg, clear=False, tap=(450, 1170), timeout=150, stall=0):
    """强制重启游戏、自动点「进入游戏」，同时等抓包数据稳定。返回本次会话目录列表。

    stall > 0 时：到该秒数还一个字节都没抓到就提前退出，交给上层自检，
    免得流量根本没进代理时白等满 timeout（换电脑排查最耗时的就是这一段）。
    """
    if pkg.lower().endswith('.huawei'):
        return restart_game_huawei(pkg, timeout=timeout)

    # b 站服：清登录态会让它弹出 B 站扫码页，脚本过不了这关 → 卡启动页、0 协议。
    # 而且 b 站服的登录态就在 ejoysdk.xml 里，留着它反而能直接进游戏。强制忽略 clear。
    if pkg == 'com.ali.croak.bilibili' and clear:
        print('   [b站服] 已忽略「清登录态」：清了会弹 B 站扫码页，脚本进不去游戏。')
        clear = False

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
    if disp:
        disp.setup(ADB[0], SER[0], pkg)
        print('   显示检测：%s' % disp.info())
    sh('am force-stop %s' % pkg)
    time.sleep(1)
    sh('logcat -c')
    sh('monkey -p %s -c android.intent.category.LAUNCHER 1' % pkg)

    if boot:
        boot.setup(ADB[0], SER[0], pkg)

    t0 = time.time()
    last_tap = 0.0
    last_size = -1
    last_change = t0
    cur = None
    while time.time() - t0 < timeout:
        el = time.time() - t0
        if el > 8 and time.time() - last_tap > 7:
            if boot:
                # 先认页面：渠道账号授权页 → 立刻停（代点没用，白等 150 秒最坑）；
                # 有可点文本（同意/允许/进入游戏）→ 点文本；都没有 → 勾协议 + 轮坐标。
                st = boot.pass_once(pkg, lambda a, b: _tap_game(a, b, pkg),
                                    int(el) // 7,
                                    virtual=bool(disp and disp.LOGICAL[0] > 0),
                                    prefer=tuple(tap))
                if st == 'login':
                    print('   [X] ' + boot.describe_login(pkg))
                    print('       → 本渠道要先人工登录一次；进到游戏主界面后重新点开始抓取。')
                    break
                if st == 'ingame':
                    print('   [%.0fs] 画面已在游戏主界面' % el)
                elif st == 'tapped':
                    print('   [%.0fs] 已点掉启动页弹窗' % el)
                elif st == 'agree':
                    print('   [%.0fs] 勾协议 → 点「进入游戏」' % el)
                else:
                    print('   [%.0fs] 点「进入游戏」' % el)
            else:
                # 没装 game_boot（旧目录只覆盖了部分文件）时退回老的盲点逻辑
                tap_candidates = [tuple(tap), (450, 960), (450, 1143)]
                pick = tap_candidates[(int(el) // 8) % len(tap_candidates)]
                _tap_game(pick[0], pick[1], pkg)
                print('   [%.0fs] 点「进入游戏」(%d,%d)' % (el, pick[0], pick[1]))
            last_tap = time.time()
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
        elif stall and el >= stall:
            print('   [%.0fs] 一个字节都没抓到，提前结束本轮（不等满 %ds）' % (el, timeout))
            break
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


def _check_abi(pkg):
    """所有渠道服通用 ABI 体检：x86 加固库在模拟器必崩，自动修成 armeabi-v7a。

    v1.7 只对华为做了 ABI 修复，但阿里聚安全的 x86 .so 是所有渠道服共用的，
    官服/米服/OPPO/b站装成 x86 一样会 SIGSEGV 闪退。这里统一检查。
    返回 True=可继续 / False=必须阻断。
    """
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import huawei_channel as hw
        hw.ADB[0] = ADB[0]
        hw.SER[0] = SER[0]
        abi = hw.primary_abi(pkg)
    except Exception as e:
        print('   [!] ABI 体检失败（不阻断）：%s' % e)
        return True
    if not abi or 'arm' in abi.lower():
        return True
    print('   [!] %s 当前 ABI=%s：阿里加固的 x86 库在模拟器会 SIGSEGV，自动修复中…' % (
        PKG_LABEL.get(pkg, pkg), abi))
    try:
        # 临时把 hw 的目标包指向当前 pkg，复用 ensure_abi 的自动重装逻辑
        old = hw.HW_PKG
        hw.HW_PKG = pkg
        try:
            r = hw.ensure_abi()
        finally:
            hw.HW_PKG = old
    except Exception as e:
        r = {'ok': False, 'error': str(e)}
    if r.get('ok') and r.get('changed'):
        print('   [OK] ABI 已改为 %s（%s），游戏数据已保留。' % (
            r.get('abi'), r.get('source', '自动提取')))
        return True
    print('   [X] ABI 自动修复失败：%s' % r.get('error', '未知错误'))
    print('       这是必崩项。可手动把游戏以 armeabi-v7a 重装，'
          '或在华为卡片「游戏 APK 路径」填本机 APK 后重试。')
    return False


def _hw_precheck(pkg):
    """华为渠道服抓取前置体检：HMS Core / 登录状态 / CPU ABI。

    ABI=x86 会必崩（阿里聚安全 x86 加固库 SIGSEGV），这里直接自动修复：
    ensure_abi() 从模拟器 pm path 抠出已装 APK，以 armeabi-v7a 覆盖重装。
    修不成就阻断抓取——继续跑只会让游戏反复闪退、白等超时。
    """
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import huawei_channel as hw
        hw.ADB[0] = ADB[0]
        hw.SER[0] = SER[0]
        st = hw.status()
    except Exception as e:
        print('   [!] 华为渠道服体检失败：%s' % e)
        return True   # 体检都失败就别在这拦，交给后面流程自己报错
    if not st.get('hms_installed'):
        print('   [!] 未安装 HMS Core，华为渠道服会卡在「安装 HMS」死循环。')
        print('       解决：把 hmscore.apk 放到 tools/apk/，或点界面上的「准备华为渠道服」。')
    if not st.get('account'):
        print('   [!] 当前没有已登录的华为账号，稍后需要你在模拟器里手动输入账号密码。')
    abi = st.get('abi') or ''
    # ABI 由 _check_abi() 对所有服统一处理（含自动修复），这里只做展示，
    # 不再重复重装，避免同一台机器修两次。
    if st.get('account'):
        print('   华为账号：%s（HMS=%s，ABI=%s）' % (
            st['account'], '有' if st.get('hms_installed') else '无', abi or '未知'))
    return True


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
        elif state == 'stuck' or state == 'timeout':
            print('   [!] 游戏停在非预期页面（常见于首次启动强制更新约 46MB，'
                  '或正在读取中）。')
            print('       请先保持模拟器联网，让游戏把更新下载完、能正常进到庭院，')
            print('       再点一次「开始抓取」；更新没跑完时自动过页逻辑进不去。')
            print('       已截图到 output/ 便于排查。')
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

def _proto_len(v):
    try:
        return len(json.dumps(v, ensure_ascii=False))
    except Exception:
        return 0


def _merge_into_proto_dir(src, proto_dir):
    """把单会话解析出的一个协议文件并进总协议目录，返回是否写入。

    不能简单地「大文件覆盖小的」：未注册的 cmd 会被解析成 unknown_NN，
    而 NN 只在单会话内按出现顺序分配——不同会话里的 unknown_53 可能是两种协议。
    直接按体积覆盖会把小的那份（博物馆 museum_list 常常就几行）整份丢掉。
    这里改成按 data 的键合并：键不冲突就都留下，冲突取更完整的那个。
    """
    name = os.path.basename(src)
    dst = os.path.join(proto_dir, name)
    if not os.path.exists(dst):
        shutil.copy2(src, dst)
        return True
    try:
        old = json.load(open(dst, encoding='utf-8'))
        new = json.load(open(src, encoding='utf-8'))
    except Exception:
        if os.path.getsize(src) > os.path.getsize(dst):
            shutil.copy2(src, dst)
            return True
        return False

    do, dn = old.get('data'), new.get('data')
    if isinstance(do, dict) and isinstance(dn, dict):
        merged = dict(do)
        changed = False
        for k, v in dn.items():
            if k not in merged or _proto_len(v) > _proto_len(merged[k]):
                merged[k] = v
                changed = True
        if changed:
            old['data'] = merged
            with open(dst, 'w', encoding='utf-8', newline='\n') as fh:
                json.dump(old, fh, ensure_ascii=False)
            return True
        return False

    if os.path.getsize(src) > os.path.getsize(dst):
        shutil.copy2(src, dst)
        return True
    return False


def parse_sessions(sessions, proto_dir):
    os.makedirs(proto_dir, exist_ok=True)
    n = 0
    for d in sorted(sessions, key=size_of_s2c):     # 大的后解析
        if size_of_s2c(d) < 1000:
            continue
        tmp = os.path.join(d, '_proto')
        rc, out = run([sys.executable, PARSE, d, tmp], timeout=60)
        if rc != 0:
            print('   [!] 解析 %s 失败：%s' % (os.path.basename(d), out.strip()[:200]))
            continue
        for f in glob.glob(os.path.join(tmp, '*.json')):
            if _merge_into_proto_dir(f, proto_dir):
                n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description='一键抓官服/米服存档')
    ap.add_argument('--port', help='模拟器 adb 端口')
    ap.add_argument('--pkg', help='包名，或 官服/米服/华为/b站（默认自动识别）')
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
        print('[X] 没找到官服/米服/华为/b站包名。确认游戏已安装，或用 --pkg 指定。')
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
    # 所有渠道服统一 ABI 体检：x86 加固库必崩，自动修（v1.7 只查华为，这里补全）
    if not _check_abi(pkg):
        return 1
    if pkg.lower().endswith('.huawei'):
        if not _hw_precheck(pkg):
            return 1
    if pkg == 'com.ali.croak.bilibili':
        print('   [b站服] 请先确认模拟器里能手动进到庭院主界面；'
              '抓包时证书会覆盖 flysdk/aligames 域名，别再勾「清登录态」。')

    ips = resolve_ips()
    print('登录服务器: %s -> %s' % (LOGIN_HOST, ', '.join(ips)))

    out_dir = os.path.join(ROOT, 'output', '一键抓官服_%s' % time.strftime('%Y%m%d_%H%M%S'))
    proto_dir = os.path.join(out_dir, '协议')
    os.makedirs(out_dir, exist_ok=True)
    extra_ips = []          # 自检时补加的劫持 IP，退出要一起撤
    got = False        # 博物馆协议是否真的抓到（--no-museum 时保持 False）

    try:
        # 抓取前无条件清理残留环境：上次异常退出 / 别的版本留下的劫持规则和证书
        # 挂载会让游戏这次进不去（米服 FlySDK 证书链校验失败卡启动页就是这么来的）。
        # 清完再抓，保证每次都从干净状态开始——覆盖所有渠道服，不只米服。
        if diag:
            try:
                pre = diag.cleanup_all(sh, HOST_PROXY, CACERT_DIR, TMP_DIR)
                if pre.get('rules_before') or pre.get('mounted_after'):
                    print('   抓取前环境清理：残留规则 %d 条已清；证书挂载=%s' % (
                        pre.get('rules_before'), '仍在（异常）' if pre.get('mounted_after') else '已卸'))
            except Exception as _e:
                print('   [!] 抓取前环境清理异常（继续尝试）：%s' % _e)

        cert_ok, cert_msg = verify_cert_pair()
        print('   证书自检：%s' % cert_msg)
        if not cert_ok:
            print('[X] 证书不配套，中止抓取（继续只会抓空）。'
                  '请完整重装抓取器：解压时 ca.pem / srv.pem / srv.key 全部选覆盖。')
            return 1

        print('\n[1/5] 启动代理...')
        old = free_proxy_port()
        if old:
            print('   已清理上次任务残留的代理进程：%s' % ', '.join(map(str, old)))
        proxy_cmd = [sys.executable, PROXY, '--listen', '8443', '--host', LOGIN_HOST]
        # b 站服登录域名未知：让它解密所有 SNI，配合全量 443 劫持才能抓到登录协议。
        if pkg in HOST_ALL_PKGS:
            proxy_cmd.append('--host-all')
            print('   [%s] 启用全域名解密（host-all）' % PKG_LABEL.get(pkg, pkg))
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
            if proxy_port_busy():
                print('    端口 8443 被别的进程占着——多半是上次任务残留的代理。')
                print('    处理：关掉抓取器再重开；仍不行就在任务管理器里结束残留的 python.exe，')
                print('    然后在网页点「一键恢复联网」清掉劫持规则。')
            return 1
        print('   代理已启动，补抓相册=%s' % ('关' if a.no_album else '开'))

        print('\n[2/5] 装证书 + 重定向登录流量...')
        if not mitm_on(ips):
            return 1

        print('\n[3/5] 重启游戏触发登录（会自动点「进入游戏」）...')
        # 渠道服启动页要先勾协议、过联运声明弹窗，40 秒经常还没点到「进入游戏」；
        # 官服/米服沿用 40 秒快速失败，其余渠道服给 70 秒。
        _stall = 40 if pkg in ('com.aligames.lxqw.hhb', 'com.ali.croak.mi') else 70
        sessions = restart_game(pkg, clear=a.clear, tap=tuple(a.tap),
                                timeout=a.timeout, stall=_stall)
        print('   本次会话 %d 个' % len(sessions))
        _ca = _game_ca_visible(pkg)
        if _ca is False:
            print('   [!] 警告：游戏进程里看不到抓包 CA —— 它没有继承 zygote 的证书挂载。')
            print('       这会导致 TLS 握手报 CERTIFICATE_UNKNOWN、0 个协议。')
            print('       请确认模拟器已 root，且系统里有 zygote/zygote64（nsenter 可用）。')

        # 一字节都没抓到 = 流量没进代理（不是解析问题）。自检整条链，能补的 IP 补上再试一轮。
        if diag and sum(size_of_s2c(d) for d in sessions) == 0:
            _hint = auth_stuck_hint(os.path.join(out_dir, '代理日志.txt'))
            if _hint:
                print('   [!] ' + _hint)
            # 注意：report() 只收 4 个参数（sh, ips, host_port, proxy_log）。
            # 旧代码这里多传了 diag.pkg_uid(...) 的返回值，但 grab_diag 里
            # 根本没有 pkg_uid 函数 —— 这是残留的幽灵调用，一走到自检就崩。
            # 直接按正确签名调用，避免 b 服/其它渠道服抓空时整轮失败。
            _ok, _real = diag.report(sh, ips, HOST_PROXY,
                                     os.path.join(out_dir, '代理日志.txt'))
            _added = [ip for ip in _real if ip not in ips]
            for ip in _added:
                _good, _err = diag.add_rule(sh, ip, HOST_PROXY)
                if _good:
                    print('   已补劫持 %s' % ip)
                else:
                    print('   [!] 补劫持 %s 失败：%s' % (ip, _err))
            if _added:
                ips = sorted(set(ips) | set(_added))
                extra_ips.extend(_added)
            elif _ok:
                # 游戏还没连（读不到目标 IP），但宿主端口是通的 -> 全量劫持兜底
                _good, _err = diag.add_all_rule(sh, HOST_PROXY)
                if _good:
                    print('   改用「出站 443 全量劫持」兜底再试一轮...')
                else:
                    print('   [!] 全量劫持规则加不上：%s' % _err)
            sessions = restart_game(pkg, clear=False, tap=tuple(a.tap),
                                    timeout=90, stall=45)
            print('   重试会话 %d 个' % len(sessions))

        if not a.no_museum:
            # 独立步骤：游戏已进主界面、代理还在，此时进博物馆触发 museum_load。
            # 不在 restart_game 循环里做，避免被定时「进入游戏」点击打断。
            print('\n   [3.5/5] 进博物馆采真实藏品（触发 museum_load）...')
            t_mus = time.time()
            opened = visit_museum(pkg)
            got = False
            if opened:
                # 博物馆数据可能走新连接（新会话目录），也可能续在旧会话上，两边都看
                for _ in range(20):        # 最多约 40 秒
                    time.sleep(2)
                    pool = list(dict.fromkeys(sessions + _sessions_since(t_mus - 5)))
                    if museum_proto_seen(pool):
                        got = True
                        print('   已收到博物馆协议（museum_list）')
                        sessions = pool
                        time.sleep(1)
                        break
                # 首次进馆偶发丢请求（模拟器 ARM 转译卡顿时，requestMuseum 没发出去），
                # 退出去再进一次通常就有了——不然藏品只能退回底稿，白抓一趟。
                if not got:
                    print('   首次进馆没等到协议，退出去再进一次...')
                    t_mus = time.time()
                    if visit_museum(pkg, retries=2):
                        for _ in range(8):     # 最多约 16 秒
                            time.sleep(2)
                            pool = list(dict.fromkeys(
                                sessions + _sessions_since(t_mus - 5)))
                            if museum_proto_seen(pool):
                                got = True
                                print('   已收到博物馆协议（museum_list，第二次触发）')
                                sessions = pool
                                break
            if not got:
                print('   [!] 没收到博物馆协议，藏品退回底稿兜底（存档仍可用）')

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
        if n == 0:
            print('   [X] 一个协议都没解析出来 —— 模拟器的流量没进代理 / 没解密成功。')
            if diag:
                for ln in diag.tail(os.path.join(out_dir, '代理日志.txt'), 8):
                    print('       代理日志| %s' % ln)
            print('       判断方法：')
            print('       · 只有「代理已启动」没有「连接来自」= 流量没进来（防火墙拦了 8443）')
            print('       · 有「连接来自」但一条「TLS 建立」都没有 = 客户端不信任证书。'
                  '先确认「CA 挂载=是（zygote 命名空间…）」；若显示 shell 命名空间或失败，'
                  '说明 Android 10+ 的每应用 mount namespace 没覆盖到，游戏看不到 CA。'
                  '再确认启动时「证书自检」那行是配套。')
            print('       · 有「TLS 建立」但 0 协议 = 解密成功但登录流程没走到，'
                  '需先手动登录进主界面再抓。')
            _hint = auth_stuck_hint(os.path.join(out_dir, '代理日志.txt'))
            if _hint:
                print('       · ' + _hint)
        key = ['client.load_role', 'item.load_items', 'album.load_all',
               'travel.load_note', 'furniture.load_furniture']
        miss = [k for k in key if k not in names]
        if miss:
            print('   [!] 缺关键协议：%s' % ', '.join(miss))
            print('       大协议有时不在缓存登录态里下发。可加 --clear 重跑（可能要重新登录）。')
        if 'misc.moment_load' not in names:
            print('   [i] 未抓到美好瞬间协议（misc.moment_load）：本次不写入该字段。')
            print('       该项在完整登录时下发；如需补抓可加 --clear 重跑，或用编辑器的「美好瞬间」页手动勾选。')

        # 存档文件名 = 导入单机版 + 昵称 + 旅行青蛙存档 + 当前时间
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
        out_json = os.path.join(out_dir, '导入单机版_%s_旅行青蛙存档_%s.json' % (nick, stamp))
        print('   存档命名：%s' % os.path.basename(out_json))
        rc, out = run([sys.executable, os.path.join(HERE, '生成实号存档.py'),
                       '--proto', proto_dir, '--base', base, '--out', out_json,
                       '--with-album', '--with-moment', '--real-fur', '--museum-from-proto',
                       '--museum-collections'], timeout=120)
        if rc != 0 or not os.path.isfile(out_json):
            print('   [X] 生成存档失败：%s' % out.strip()[:500])
            return 1

        # 顺手留一份解密明文（原始数据），方便排查/二次编辑。明文没有 format 信封，不能导入游戏。
        muse_line = None
        try:
            sys.path.insert(0, os.path.join(ROOT, 'lib'))
            import tf as _tf
            _raw, _env = _tf.load(out_json)
            raw_json = os.path.join(out_dir, '%s_旅行青蛙官服存档_原始数据_%s.json' % (nick, stamp))
            json.dump(_raw, open(raw_json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
            # 汇报博物馆藏品到底是"账号真实点亮"还是"底稿兜底"。
            # 判据看最终协议目录里有没有 museum_list 数组——这才是生成存档真正用到的数据。
            _mids = set()
            _cfg = os.path.join(ROOT, 'data', 'config_json', 'museumData_json.json')
            if os.path.isfile(_cfg):
                for _m in json.load(open(_cfg, encoding='utf-8')).values():
                    for _v in str((_m or {}).get('collection_id') or '').split(','):
                        _v = _v.strip()
                        if _v.isdigit():
                            _mids.add(int(_v))
            _lit = sorted(set(_raw.get('collections') or []) & _mids)
            _real = False
            for _f in glob.glob(os.path.join(proto_dir, '*.json')):
                try:
                    _d = (json.load(open(_f, encoding='utf-8')) or {}).get('data')
                except Exception:
                    continue
                if isinstance(_d, dict) and isinstance(_d.get('museum_list'), list):
                    _real = True
                    break
            if _mids:
                muse_line = '博物馆藏品 %d/%d 件 %s（%s）' % (
                    len(_lit), len(_mids), _lit,
                    '账号真实点亮' if _real else '底稿兜底，未抓到真实数据')
        except Exception as _e:
            raw_json = None
            print('   [!] 原始数据备份失败：%s' % _e)

        print('\n' + '=' * 68)
        print('  完成！可导入存档：')
        print('  %s' % out_json)
        if muse_line:
            print('  %s' % muse_line)
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
        # 无条件清理：异常退出时 ON[0] 可能还没置位，但规则可能已经加了一部分。
        # 米服卡启动页就是因为这里「ON 为假就跳过清理」留下了劫持规则。
        mitm_off(ips if 'ips' in dir() else KNOWN_IPS)


def _fatal(exc):
    """把异常原因写到日志 + 用 ASCII 安全方式打印，避免老系统 GBK 控制台把中文堆栈打印成空白。"""
    import traceback as _tb
    # 日志文件用 UTF-8 落盘，用户发我这个文件就能看到完整堆栈
    log_path = os.path.join(ROOT, 'grab_error.log')
    try:
        with open(log_path, 'w', encoding='utf-8') as _f:
            _f.write('TravelFrog Grabber fatal error\n')
            _f.write('Python %s\n' % sys.version)
            _f.write(_tb.format_exc())
    except Exception:
        pass
    # 控制台只打 ASCII 安全的摘要，避免 GBK 控制台打印中文抛异常导致堆栈完全看不见
    print('')
    print('=== FATAL ERROR (see grab_error.log for full traceback) ===')
    print('Python: %s' % sys.version.replace('\n', ' '))
    try:
        _msg = (str(exc) or exc.__class__.__name__).encode('ascii', 'backslashreplace').decode('ascii')
        print('%s: %s' % (exc.__class__.__name__, _msg[:500]))
    except Exception:
        print('exception type: %s' % type(exc).__name__)
    print('Log: %s' % log_path)
    print('==========================================================')
    sys.stdout.flush()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as _exc:
        try:
            _fatal(_exc)
        except Exception:
            pass
        raise SystemExit(1)
