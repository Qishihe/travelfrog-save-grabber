# -*- coding: utf-8 -*-
"""幂等补丁：把华为渠道服支持打进 scripts/grab_official.py。

为什么要有这个脚本：
    这个文件在编辑过程中被人工改动覆盖过两次，导致华为相关改动整段丢失。
    把改动写成幂等的字符串替换，任何一次被覆盖后重跑本脚本即可恢复，
    不用再逐段手工核对。

用法：python _apply_hw_patch.py  （在项目根目录执行）
"""
import io
import sys

P = 'scripts/grab_official.py'

try:
    s = io.open(P, encoding='utf-8').read()
except Exception as e:
    print('[X] 读不到 %s：%s' % (P, e))
    raise SystemExit(1)

applied = []
skipped = []


def sub(old, new, tag):
    """替换一次；新内容已在就跳过（避免把「old 是 new 前缀」的段落重复替换）。"""
    global s
    if new in s or tag in s:
        skipped.append(tag)
    elif old in s:
        s = s.replace(old, new, 1)
        applied.append(tag)
    else:
        print('[!] 未匹配，需人工检查：%s' % tag)


# 1) 文件头
sub('"""一键抓官服（也支持米服）存档',
    '"""一键抓官服（也支持米服、华为渠道服）存档', 'doc:华为渠道服')

sub('  1. 找 adb、自动探测模拟器端口、自动识别包名（官服 com.aligames.lxqw.hhb 优先，米服 com.ali.croak.mi 也认）',
    '  1. 找 adb、自动探测模拟器端口、自动识别包名（官服 com.aligames.lxqw.hhb 优先，\n'
    '     米服 com.ali.croak.mi、华为渠道服 com.Aligames.lxqw.huawei 也认）', 'doc:pkg')

sub('  5. 强制重启游戏触发全量登录，自动点「进入游戏」；可选补抓相册图层',
    '  5. 强制重启游戏触发全量登录，自动点「进入游戏」；可选补抓相册图层\n'
    '     —— 华为渠道服走 huawei_channel：自动过隐私协议/联运声明/启动页勾选；\n'
    '        若弹华为账号登录页则明确报错，等你手动登录后重跑', 'doc:step5')

sub('    --pkg <包名>    指定包名（默认自动识别）',
    '    --pkg <包名>    指定包名，或写 官服/米服/华为（默认自动识别）', 'doc:pkgarg')

# 2) 包名表
sub("""PKGS = ['com.aligames.lxqw.hhb', 'com.ali.croak.mi']  # 官服优先（两个都是官方服）
PKG_LABEL = {'com.aligames.lxqw.hhb': '阿里官服', 'com.ali.croak.mi': '米服'}
PKG_ALIAS = {
    '官服': 'com.aligames.lxqw.hhb', 'aligames': 'com.aligames.lxqw.hhb',
    'official': 'com.aligames.lxqw.hhb', 'ali': 'com.aligames.lxqw.hhb',
    '米服': 'com.ali.croak.mi', 'mi': 'com.ali.croak.mi',
}""",
    """PKGS = ['com.aligames.lxqw.hhb', 'com.ali.croak.mi', 'com.Aligames.lxqw.huawei']  # 官服/米服优先
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
HW_PKG = 'com.Aligames.lxqw.huawei'""", 'HW_PKG')

# 3) installed_pkgs（华为包名是大写 A，grep 必须 -i）
sub("""def installed_pkgs():
    \"\"\"列出模拟器里已安装的所有官服/米服包名（去重、按 PKGS 顺序）。\"\"\"
    out = sh("pm list packages | grep -E 'aligames|croak'")
    have = set(l.strip().replace('package:', '') for l in out.splitlines() if l.strip())
    got = [p for p in PKGS if p in have]
    # 认不出的渠道包（只要带 aligames/croak）也列在后面，交给 --pkg 硬指定
    got += sorted(p for p in have if p not in PKGS)
    return got""",
    """def installed_pkgs():
    \"\"\"列出模拟器里已安装的所有官服/米服/华为渠道服包名（去重、按 PKGS 顺序）。\"\"\"
    # 华为渠道服包名是 com.Aligames.lxqw.huawei（大写 A），不区分大小写才不会漏
    out = sh("pm list packages | grep -i -E 'aligames|croak'")
    have = set(l.strip().replace('package:', '') for l in out.splitlines() if l.strip())
    low = {p.lower(): p for p in have}
    got = [low[p.lower()] for p in PKGS if p.lower() in low]
    known = {p.lower() for p in PKGS}
    # 认不出的渠道包（只要带 aligames/croak）也列在后面，交给 --pkg 硬指定
    got += sorted(p for p in have if p.lower() not in known)
    return got""", 'grep -i -E')

# 4) 设备探测：不再静默 fallback 到别的实例
sub("""def detect_serial(want_port=None):
    run([ADB[0], 'start-server'])""",
    """def _devices_online():
    rc, out = run([ADB[0], 'devices'])
    return [l.split()[0] for l in out.splitlines() if '\\tdevice' in l]


def _has_game(serial, want_pkg=None):
    \"\"\"这台设备是否可以执行命令、并且装了目标游戏（不装游戏的设备点了也是白点）。\"\"\"
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
""", '_has_game')

sub("""    known = ['16384', '16416', '16448', '7555', '5557', '5555', '5556',
             '62001', '62025', '21503']
    ports = [want_port] if want_port else known
    for p in ports:
        target = '127.0.0.1:%s' % p
        run([ADB[0], 'connect', target])
        rc, dev = run([ADB[0], '-s', target, 'shell', 'pm list packages'])
        # 这个端口要真的能执行命令（装了游戏）才算数
        if rc == 0 and ('aligames' in dev or 'croak' in dev):
            return target
    # 端口漂移：按模拟器进程反查到的端口再试一遍
    if not want_port:
        for p in _emu_listening_ports():
            if str(p) in ports:
                continue
            target = '127.0.0.1:%d' % p
            run([ADB[0], 'connect', target])
            rc, dev = run([ADB[0], '-s', target, 'shell', 'pm list packages'])
            if rc == 0 and ('aligames' in dev or 'croak' in dev):
                return target
    # 退而求其次：任何一个在线设备
    rc, out = run([ADB[0], 'devices'])
    online = [l.split()[0] for l in out.splitlines() if '\\tdevice' in l]
    return online[0] if online else None""",
    """    # 自动探测：已知端口 -> 模拟器进程占用的端口 -> 已在线设备
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
    return None""", '自动探测：已知端口')

# 5) restart_game 华为分支
sub("""def restart_game(pkg, clear=False, tap=(450, 1170), timeout=150):
    \"\"\"强制重启游戏、自动点「进入游戏」，同时等抓包数据稳定。返回本次会话目录列表。\"\"\"
    if clear:""",
    """def restart_game(pkg, clear=False, tap=(450, 1170), timeout=150):
    \"\"\"强制重启游戏、自动点「进入游戏」，同时等抓包数据稳定。返回本次会话目录列表。\"\"\"
    if pkg.lower().endswith('.huawei'):
        return restart_game_huawei(pkg, timeout=timeout)

    if clear:""", 'restart_game_huawei(pkg, timeout=timeout)')

# 6) 新函数块（插在 size_of_s2c 之前）
NEW_FUNCS = '''def _drain_capture(t0, max_wait=90):
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


def size_of_s2c(d):'''

if 'def _drain_capture' not in s:
    sub('def size_of_s2c(d):', NEW_FUNCS, 'def _drain_capture')
else:
    skipped.append('def _drain_capture')

# 7) main：设备探测带目标包 + 华为体检
sub("""    SER[0] = detect_serial(a.port)
    if not SER[0]:
        print('[X] 连不上模拟器。先打开 MuMu/雷电并启动游戏，或用 --port 指定端口。')
        return 1""",
    """    want = PKG_ALIAS.get(str(a.pkg).strip().lower(), a.pkg) if a.pkg else None
    SER[0] = detect_serial(a.port, want)
    if not SER[0]:
        if a.port:
            print('[X] 端口 %s 上没有可用的游戏设备（连不上，或那台没装目标服）。\\n'
                  '    多开模拟器时请确认端口对应的是装了该服的实例。' % a.port)
        else:
            print('[X] 连不上模拟器。先打开 MuMu/雷电并启动游戏，或用 --port 指定端口。')
        return 1""", 'want = PKG_ALIAS.get')

sub("""    print('包名: %s（%s）' % (pkg, PKG_LABEL.get(pkg, '未知渠道')))""",
    """    print('包名: %s（%s）' % (pkg, PKG_LABEL.get(pkg, '未知渠道')))
    if pkg.lower().endswith('.huawei'):
        _hw_precheck(pkg)""", '_hw_precheck(pkg)')

sub("""    ap.add_argument('--pkg', help='包名（默认自动识别）')""",
    """    ap.add_argument('--pkg', help='包名，或 官服/米服/华为（默认自动识别）')""", '官服/米服/华为')

# ---------------- 博物馆导航修复（2026-11-27）----------------
# 旧版「进博物馆」用的是盲点坐标 (820,65)/(693,60)/(240,915)，第三下打在庭院空白处，
# 博物馆页从没打开过，museum_load 不下发，藏品只能用底稿填满 20 件。
# 现在导航逻辑搬到 scripts/museum.py（独立文件，不会被整文件覆盖冲掉），
# 这里只保留幂等的函数级替换，任何一次被覆盖后重跑本脚本即可恢复。
def _patch_museum(txt):
    import re as _re
    tags = []

    # 1) visit_museum：旧盲点函数 → 薄代理
    NEW_VM = '''def visit_museum(pkg=None, retries=3):
    """进博物馆页触发服务器下发 museum_load（实现见 scripts/museum.py）。"""
    try:
        import museum
        museum.setup(ADB[0], SER[0])
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

'''
    m = _re.search(r'def visit_museum\(\)[^\n]*\n.*?(?=\ndef |\Z)', txt, _re.S)
    if m and '820, 65' in m.group(0):
        txt = txt[:m.start()] + NEW_VM + txt[m.end():]
        tags.append('博物馆:visit_museum → museum 模块')
    elif 'import museum' in txt or 'museum.visit(' in txt:
        pass
    else:
        print('[!] 博物馆：没找到旧的 visit_museum，需人工检查')

    # 2) parse_sessions：unknown_NN 同号不同义时，别再用「大文件覆盖小的」
    NEW_PS = '''def _proto_len(v):
    try:
        return len(json.dumps(v, ensure_ascii=False))
    except Exception:
        return 0


def _merge_into_proto_dir(src, proto_dir):
    """把单会话解析出的协议文件并进总目录，返回是否写入。

    未注册的 cmd 会被解析成 unknown_NN，而 NN 只在单会话内按出现顺序分配——
    不同会话里的 unknown_53 可能是两种协议。按体积覆盖会把小的那份
    （博物馆 museum_list 往往只有几行）整份丢掉，所以改成按 data 的键合并。
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
            with open(dst, 'w', encoding='utf-8', newline='\\n') as fh:
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

'''
    m = _re.search(r'def parse_sessions\(.*?(?=\ndef )', txt, _re.S)
    if m and '_merge_into_proto_dir' not in m.group(0):
        txt = txt[:m.start()] + NEW_PS + txt[m.end():]
        tags.append('博物馆:parse_sessions 改按 data 键合并')
    elif '_merge_into_proto_dir' in txt:
        pass
    else:
        print('[!] 博物馆：没找到 parse_sessions，需人工检查')

    # 3) 主流程：等待判据从「s2c 变大」改成「出现 museum_list 数组」
    OLD_BLOCK = """            size_before = sum(size_of_s2c(d) for d in sessions)
            if visit_museum():
                # 等 s2c 增长（museum_load 下发）或最多 20 秒
                for _ in range(10):
                    time.sleep(2)
                    if sum(size_of_s2c(d) for d in sessions) > size_before:
                        print('   已收到博物馆协议数据')
                        time.sleep(2)
                        break
                else:
                    print('   [!] 未检测到 museum_load 下发，博物馆藏品将用兜底')"""
    NEW_BLOCK = """            t_mus = time.time()
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
                print('   [!] 没收到博物馆协议，藏品退回底稿兜底（存档仍可用）')"""
    if OLD_BLOCK in txt:
        txt = txt.replace(OLD_BLOCK, NEW_BLOCK, 1)
        tags.append('博物馆:等待判据改成 museum_list')
    elif 'museum_proto_seen(pool)' in txt:
        # 已经打过补丁：把「只等 24 秒」的旧版本就地升级成「40 秒 + 二次触发」
        if '首次进馆没等到协议' not in txt:
            txt = txt.replace('for _ in range(12):        # 最多约 24 秒',
                              'for _ in range(20):        # 最多约 40 秒', 1)
            _OLD_TAIL = """            if not got:
                print('   [!] 没收到博物馆协议，藏品退回底稿兜底（存档仍可用）')"""
            _NEW_TAIL = """                # 首次进馆偶发丢请求（模拟器 ARM 转译卡顿时，requestMuseum 没发出去），
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
""" + _OLD_TAIL
            if _OLD_TAIL in txt:
                txt = txt.replace(_OLD_TAIL, _NEW_TAIL, 1)
                tags.append('博物馆:补上二次进馆')
    else:
        print('[!] 博物馆：没找到旧的等待逻辑，需人工检查')

    return txt, tags


s, _tags = _patch_museum(s)
applied.extend(_tags)

# 去重：重复跑补丁容易叠出重复段落
_DUPS = [
    ("""     —— 华为渠道服走 huawei_channel：自动过隐私协议/联运声明/启动页勾选；
        若弹华为账号登录页则明确报错，等你手动登录后重跑
     —— 华为渠道服走 huawei_channel：自动过隐私协议/联运声明/启动页勾选；
        若弹华为账号登录页则明确报错，等你手动登录后重跑""",
     """     —— 华为渠道服走 huawei_channel：自动过隐私协议/联运声明/启动页勾选；
        若弹华为账号登录页则明确报错，等你手动登录后重跑"""),
    ("""    if pkg.lower().endswith('.huawei'):
        _hw_precheck(pkg)
    if pkg.lower().endswith('.huawei'):
        _hw_precheck(pkg)""",
     """    if pkg.lower().endswith('.huawei'):
        _hw_precheck(pkg)"""),
]
for _a, _b in _DUPS:
    if _a in s:
        s = s.replace(_a, _b, 1)
        applied.append('去重')

io.open(P, 'w', encoding='utf-8', newline='\n').write(s)

print('已应用 %d 处，已存在 %d 处' % (len(applied), len(skipped)))
for a in applied:
    print('  + %s' % a)
if skipped:
    print('已跳过（本来就有）：%s' % '、'.join(skipped))

# 自检
need = ['HW_PKG', '_has_game', 'restart_game_huawei', '_hw_precheck', '_drain_capture',
        "grep -i -E 'aligames|croak'", 'museum.visit(', '_merge_into_proto_dir',
        'museum_proto_seen']
miss = [n for n in need if n not in s]
if miss:
    print('[X] 自检失败，仍缺：%s' % miss)
    raise SystemExit(2)
print('自检通过')
