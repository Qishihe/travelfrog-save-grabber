# -*- coding: utf-8 -*-
"""抓不到流量时的自检与自愈。

只在「一个字节都没抓到」的异常路径上运行，成功路径完全不受影响。
所有函数都接收调用方传入的 sh() 回调，不自己持有 adb 状态，方便复用/单测。

背景：抓包链是
    游戏 -> 模拟器 iptables(DNAT 443) -> 宿主 10.0.2.2:8443 代理 -> 解析
前两环任何一环断了，最后的表现都是「解析出 0 个协议文件」，很容易被误判成解析问题。
这里把每一环探一遍、打印结论，能自愈的顺手补上（补真实 IP 的 DNAT）。
"""

# 本进程加过的「全量 443」规则，退出时要知道删哪条
ALL_ON = [False]


def _ip_from_hex(h):
    """把 /proc/net/tcp 里 8 位小端十六进制转成点分 IP。"""
    if len(h) != 8:
        return ''
    try:
        b = bytes.fromhex(h)
    except ValueError:
        return ''
    return '.'.join(str(x) for x in reversed(b))


def tcp_targets(text):
    """解析 /proc/net/tcp，返回远端的 [(ip, port)]（已按出现顺序去重）。"""
    out, seen = [], set()
    for ln in (text or '').splitlines()[1:]:
        f = ln.split()
        if len(f) < 4:
            continue
        rem = f[2]
        if ':' not in rem:
            continue
        ip_h, port_h = rem.rsplit(':', 1)
        ip = _ip_from_hex(ip_h)
        try:
            port = int(port_h, 16)
        except ValueError:
            continue
        if ip and (ip, port) not in seen:
            seen.add((ip, port))
            out.append((ip, port))
    return out


def _is_local(ip):
    return ip.startswith(('10.', '127.', '0.', '169.254.'))


def real_web_ips(sh, dport=443):
    """模拟器里当前真正连着的公网 443 目标。游戏还没连上时返回空列表。"""
    ips = []
    for f in ('/proc/net/tcp', '/proc/net/tcp6'):
        txt = sh('cat %s 2>/dev/null' % f)
        for ip, port in tcp_targets(txt):
            if port == dport and not _is_local(ip) and ip not in ips:
                ips.append(ip)
    return ips


def probe(sh, host_port, wait=2):
    """从模拟器内部探宿主代理端口是否可达。返回 (ok, 说明)。

    Windows 防火墙拦了入站、或模拟器宿主地址不是 10.0.2.2 时，这里必定失败。
    """
    host, _, port = host_port.partition(':')
    last = ''
    for tool in ('toybox nc', 'nc'):
        r = sh('echo | %s -w %d %s %s; echo rc=$?' % (tool, wait, host, port))
        last = (r or '').strip()
        if 'rc=0' in last:
            return True, 'ok'
        if 'not found' in last or 'inaccessible' in last:
            continue          # 换一个工具再试
        break
    return False, last[:120]


def add_rule(sh, ip, host_port):
    """按目标 IP 加 DNAT，并回读规则确认真的写进去了。返回 (ok, 错误)。"""
    sh('iptables -t nat -D OUTPUT -p tcp -d %s --dport 443 -j DNAT '
       '--to-destination %s 2>/dev/null' % (ip, host_port))
    r = sh('iptables -t nat -A OUTPUT -p tcp -d %s --dport 443 -j DNAT '
           '--to-destination %s 2>&1; echo rc=$?' % (ip, host_port))
    if 'rc=0' not in (r or ''):
        return False, (r or '').strip()[:120]
    chk = sh("iptables -t nat -S OUTPUT 2>/dev/null | grep -c -- '-d %s'" % ip)
    return chk.strip() not in ('', '0'), ''


def add_all_rule(sh, host_port):
    """兜底：不认 IP，把出站 443 全劫持到代理（代理按 SNI 分流，其余纯透传）。"""
    sh('iptables -t nat -D OUTPUT -p tcp --dport 443 -j DNAT '
       '--to-destination %s 2>/dev/null' % host_port)
    r = sh('iptables -t nat -A OUTPUT -p tcp --dport 443 -j DNAT '
           '--to-destination %s 2>&1; echo rc=$?' % host_port)
    if 'rc=0' not in (r or ''):
        return False, (r or '').strip()[:120]
    ALL_ON[0] = True
    return True, ''


def cleanup(sh, host_port, ips=()):
    """撤掉本次补的规则（含全量兜底），避免退出后模拟器断网。"""
    for ip in ips:
        sh('iptables -t nat -D OUTPUT -p tcp -d %s --dport 443 -j DNAT '
           '--to-destination %s 2>/dev/null' % (ip, host_port))
    if ALL_ON[0]:
        sh('iptables -t nat -D OUTPUT -p tcp --dport 443 -j DNAT '
           '--to-destination %s 2>/dev/null' % host_port)
        ALL_ON[0] = False


def tail(path, n=8):
    """取日志最后 n 行（失败时直接贴在控制台，省得用户找文件）。"""
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return [ln.rstrip() for ln in f.read().splitlines()[-n:]]
    except Exception:
        return []


def log_stats(path):
    """统计代理日志：收到几条连接、几条真正解密。"""
    conns = mitm = 0
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for ln in f:
                if '连接来自' in ln:
                    conns += 1
                elif 'TLS 建立' in ln:
                    mitm += 1
    except Exception:
        pass
    return conns, mitm


def report(sh, ips, host_port, proxy_log):
    """把整条链的三环都探一遍，打印人话结论。返回 (可达, 真实IP列表)。"""
    ok, detail = probe(sh, host_port)
    print('   [诊断] 模拟器 -> 宿主 %s：%s'
          % (host_port, '通' if ok else '不通（%s）' % (detail or '无响应')))

    real = real_web_ips(sh)
    if real:
        miss = [ip for ip in real if ip not in ips]
        print('   [诊断] 游戏当前连的 443 目标：%s' % ', '.join(real))
        if miss:
            print('   [诊断] %d 个不在劫持名单里：%s（下面自动补）'
                  % (len(miss), ', '.join(miss)))
    else:
        print('   [诊断] 模拟器里没有活动的 443 连接（游戏没启动/没登录，或走了别的端口）')

    conns, mitm = log_stats(proxy_log)
    print('   [诊断] 代理日志：收到 %d 条连接，解密 %d 条' % (conns, mitm))

    if not ok:
        print('   [诊断] 结论：模拟器连不到宿主 8443 —— 多半是 Windows 防火墙拦了'
              '（重跑时若弹「是否允许」，勾「专用+公用」再点允许），'
              '或模拟器不是 MuMu 的 10.0.2.2 网络。')
    elif conns == 0:
        print('   [诊断] 结论：能连到 8443，但游戏流量没被劫持过来 —— 登录服 IP 与劫持名单不一致。')
    elif mitm == 0:
        print('   [诊断] 结论：连接到了代理但 TLS 没建立 —— CA 证书没生效（挂载失败/未信任）。')
    else:
        print('   [诊断] 结论：流量已解密，问题在解析环节（把「代理日志.txt」发作者）。')
    return ok, real
