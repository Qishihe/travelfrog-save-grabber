# -*- coding: utf-8 -*-
"""华为渠道服辅助模块 —— 把「装包 → 登录 → 过协议 → 进游戏 → 换号」这套手工流程固化下来。

为什么单独一个文件：
    官服/米服只需要「重启 → 点进入游戏」；华为渠道服多了三道门，全塞进 grab_official.py
    会让主流程难以阅读，而且换号（清系统账号库）是个破坏性操作，必须能被 UI 单独调用。

子命令（都给 app.py / grab_official.py 调用，也支持手动跑）：
    status   查当前状态：包是否安装、HMS Core、CPU ABI、系统里的华为账号
    logout   一键退出华为账号（删 AccountManager 账号库 + 重启模拟器）
    prepare  补齐运行条件：装 HMS Core、必要时以 armeabi-v7a 重装游戏
    launch   启动游戏并自动越过协议页，直到主界面；遇到账号登录页则明确报告「需要手动登录」

依赖：adb（优先用包内 tools/platform-tools/adb.exe）与模拟器 root。
只操作本机模拟器，不联网、不上传任何数据。
"""
import argparse
import json
import os
import re
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

# 华为服也会被 MuMu KeepAlive 放进虚拟显示：过协议/点进入游戏的截图与点击
# 必须跟游戏窗口走同一块屏，否则会停在启动页或误判黑屏。
try:
    import game_display as disp
except Exception:
    disp = None

HW_PKG = 'com.Aligames.lxqw.huawei'          # 华为渠道服
HMS_PKG = 'com.huawei.hwid'                  # 华为移动服务（HMS Core）
HW_ACCOUNT_TYPE = 'com.huawei.hwid'

CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

ADB_CANDIDATES = [
    os.environ.get('ADB'),
    os.path.join(ROOT, 'tools', 'platform-tools', 'adb.exe'),
    r'D:\Program Files\Netease\MuMuPlayer-12.0\nx_main\adb.exe',
    r'D:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe',
    r'C:\Program Files\Netease\MuMuPlayer-12.0\nx_main\adb.exe',
    r'C:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe',
]

HMS_APK_CANDIDATES = [
    os.path.join(ROOT, 'tools', 'apk', 'hmscore.apk'),
    os.path.join(ROOT, 'tools', 'apk', 'HMS_Core.apk'),
    r'D:\_apk_tmp\hmscore.apk',
]

# 已知的 adb 端口（MuMu 会从 16384 漂到 16416/16448）
KNOWN_PORTS = ['16384', '16448', '16416', '7555', '5557', '5555',
               '62001', '21503']

# 900x1600 基准下的固定坐标（实测有效）
REF_W, REF_H = 900, 1600
TAP_AGREE_CHECKBOX = (141, 1305)     # 启动页「我已阅读并同意」复选框中心
TAP_ENTER_GAME = (450, 1143)         # 启动页「点击进入游戏」
TAP_LOGIN_AGREE = (795, 950)         # 联运服务声明弹窗的「同意」
TAP_PRIVACY_AGREE = (656, 1008)      # 首次隐私协议弹窗的「同意」

ADB = [None]
SER = [None]


# ---------------------------------------------------------------- adb 基础
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
    """连上模拟器，返回 serial；优先找装了华为渠道服的那台。"""
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
        if HW_PKG.lower() in out.lower():
            SER[0] = target
            return target
        if 'aligames' in out.lower() or 'croak' in out.lower():
            if fallback is None:
                fallback = target
    SER[0] = fallback
    return fallback


def ensure_root():
    ident = sh('id')
    if 'uid=0' in ident:
        return True, '已是 root'
    run([ADB[0], '-s', SER[0], 'root'], 30)
    time.sleep(3)
    # 注意：adb root 后不要 disconnect，会丢 root 上下文
    for _ in range(6):
        ident = sh('id')
        if 'uid=0' in ident:
            return True, '已提权为 root'
        time.sleep(1)
    return False, ident[:120] or 'adb root 失败'


def screen_size():
    m = re.search(r'(\d+)x(\d+)', sh('wm size'))
    if not m:
        return REF_W, REF_H
    return int(m.group(1)), int(m.group(2))


def _scaled(xy):
    w, h = screen_size()
    return int(xy[0] * w / REF_W), int(xy[1] * h / REF_H)


def tap(xy):
    x, y = _scaled(xy)
    if disp:
        disp.setup(ADB[0], SER[0], HW_PKG)
        if disp.tap_base(x, y):
            return
    sh('input tap %d %d' % (x, y))


def focus_activity():
    """读焦点窗口；默认屏第一行常是 null，取最后一条有效记录。"""
    if disp:
        disp.setup(ADB[0], SER[0], HW_PKG)
        hit = disp.focus()
        if hit:
            return hit
    out = sh('dumpsys window | grep -i mCurrentFocus')
    hits = re.findall(r'u0\s+([^\s}]+)', out)
    for h in reversed(hits):
        if h and h.lower() not in ('null', 'nan'):
            return h
    return hits[0] if hits else ''


def ui_texts():
    """dump 当前界面文本；自绘界面（游戏启动页）通常返回空串。"""
    sh('uiautomator dump /sdcard/_hw_ui.xml >/dev/null 2>&1', t=30)
    xml = sh('cat /sdcard/_hw_ui.xml 2>/dev/null', t=30)
    return set(re.findall(r'text="([^"]+)"', xml))


def pkg_installed(pkg):
    out = sh("pm list packages | grep -i '%s'" % pkg)
    return pkg.lower() in out.lower()


def primary_abi(pkg):
    out = sh("dumpsys package %s | grep -E 'primaryCpuAbi|secondaryCpuAbi'" % pkg)
    m = re.search(r'primaryCpuAbi=(\S+)', out)
    return m.group(1) if m else ''


# ---------------------------------------------------------------- 账号
def accounts():
    """系统 AccountManager 里的账号列表（含类型）。"""
    out = sh('dumpsys account')
    res = []
    for m in re.finditer(r'Account \{name=([^,]+),\s*type=([^\s}]+)\}', out):
        res.append({'name': m.group(1).strip(), 'type': m.group(2).strip()})
    return res


def huawei_accounts():
    return [a for a in accounts() if a['type'] == HW_ACCOUNT_TYPE]


def account_count():
    out = sh('dumpsys account')
    m = re.search(r'Accounts:\s*(\d+)', out)
    return int(m.group(1)) if m else len(accounts())


# ---------------------------------------------------------------- 退出账号
def _have_sqlite3():
    """模拟器里有没有 sqlite3 二进制（不少模拟器镜像没带）。"""
    return bool(sh('command -v sqlite3 2>/dev/null').strip()
                or sh('ls /system/bin/sqlite3 /system/xbin/sqlite3 2>/dev/null').strip())


def _sqlite(db, sql):
    """执行 sqlite3 语句；库/表不存在时静默跳过（不同 Android 版本表结构有差异）。

    这里以前写成 rc, out = sh(...)，但 sh() 只返回输出字符串，
    于是每次「退出华为账号」都在解包时抛
    ValueError: not enough values to unpack —— 这个功能从未真正可用过。
    """
    try:
        rc, out = adb('shell', 'sqlite3 %s "%s" 2>&1' % (db, sql))
    except Exception as e:
        return False, str(e)
    low = (out or '').lower()
    if 'no such table' in low or 'unable to open' in low or 'no such file' in low:
        return False, out
    return rc == 0, out


DE_DB = '/data/system_de/0/accounts_de.db'
CE_DB = '/data/system_ce/0/accounts_ce.db'


def db_account_count(db=DE_DB):
    """账号库里还剩几条华为账号；查询失败返回 None（不可判定）。"""
    try:
        rc, out = adb('shell',
                      "sqlite3 %s \"select count(*) from accounts where type='%s';\""
                      % (db, HW_ACCOUNT_TYPE))
    except Exception:
        return None
    t = (out or '').strip()
    if t.isdigit():
        return int(t)
    return None


def db_remaining():
    """DE / CE 两个库里华为账号的剩余条数；都读不到时返回 None。"""
    de = db_account_count(DE_DB)
    ce = db_account_count(CE_DB)
    if de is None and ce is None:
        return None
    return (de or 0) + (ce or 0)


def _reset_db():
    """备份后删库。系统下次启动会自动重建空库。

    没有 sqlite3 二进制、或 sqlite 删了但残留时用这条兜底。
    副作用：系统里所有账号一起清掉 —— 对「换号抓取」正是想要的结果。
    """
    for db in (DE_DB, CE_DB):
        sh('cp -f %s /data/local/tmp/%s.bak 2>/dev/null' % (db, os.path.basename(db)))
        sh('rm -f %s %s-wal %s-shm' % (db, db, db))


def _clear_db():
    """从两个账号库里删掉华为账号，返回 (剩余条数, 明细步骤)。

    DE 库有 grants / visibility / shared_accounts 关联表，先清子表再删主记录；
    CE 库的 accounts 上挂了级联触发器，authtokens / extras 会跟着走。
    """
    sel = "select _id from accounts where type='%s'" % HW_ACCOUNT_TYPE
    steps = []
    for tbl in ('grants', 'visibility'):
        rc, out = _sqlite(DE_DB, "delete from %s where accounts_id in (%s);" % (tbl, sel))
        steps.append('%s:%s' % (tbl, 'ok' if rc else (out or 'skip')[:40]))
    for db, tbl in ((DE_DB, 'shared_accounts'), (DE_DB, 'accounts'), (CE_DB, 'accounts')):
        rc, out = _sqlite(db, "delete from %s where type='%s';" % (tbl, HW_ACCOUNT_TYPE))
        steps.append('%s.%s:%s' % (os.path.basename(db), tbl,
                                   'ok' if rc else (out or 'skip')[:40]))
    return db_remaining(), steps


def logout(reboot=True, wait=300, soft=True):
    """一键退出系统里的华为账号。

    华为账号归 AccountManagerService 管，pm clear 清不掉，必须动账号库：
      DE 库 accounts / grants / visibility / shared_accounts
      CE 库 accounts / authtokens / extras（删除走级联触发器）

    v2.3 修了两个把它卡死的坑：
    1. 旧代码删完用 dumpsys 判断成败。dumpsys 读的是系统服务**内存缓存**，
       库已经删空它也照样显示有账号 —— 于是判定「删除失败」直接 return，
       永远走不到重启，账号永远不消失（这就是「还是清除不了」）。
       现在改成查库计数：库空就算删成功。
    2. 重启后 adb 会一直停在 offline，旧代码只循环 getprop、不重连，
       白等 180 秒后报超时。现在会主动 disconnect/connect 等它回来。
    """
    if not SER[0]:
        return {'ok': False, 'error': '没连上模拟器'}
    ok, msg = ensure_root()
    if not ok:
        return {'ok': False, 'error': '需要 root 才能操作系统账号库：%s' % msg}

    before = huawei_accounts()
    db_before = db_remaining()
    if not before and not db_before:
        return {'ok': True, 'changed': False, 'accounts': [],
                'message': '当前没有登录中的华为账号'}

    sh('am force-stop %s' % HW_PKG)
    sh('am force-stop %s' % HMS_PKG)
    time.sleep(1)

    method = 'sqlite'
    steps = []
    if _have_sqlite3():
        left, steps = _clear_db()
    else:
        left = None
        _reset_db()
        left = db_remaining()
        if left is None:
            left = 0
        method = 'reset_db'

    if left is None:
        return {'ok': False, 'accounts': before, 'steps': steps,
                'error': '账号库读写失败（sqlite3 打不开 %s）。请确认模拟器已 root。' % DE_DB}

    if left > 0:
        # sqlite 删了但库里还有 → 备份后整库重置
        _reset_db()
        method = 'reset_db'
        left = db_remaining()
        if left is None:
            left = 0

    if left > 0:
        return {'ok': False, 'accounts': before, 'steps': steps,
                'error': '账号库清了 %d 条后仍有残留，可能存在 -wal 未提交。'
                         '请在模拟器里手动重启一次再点退出。' % left}

    cached = huawei_accounts()
    detail = {'db_cleared': True, 'method': method, 'steps': steps,
              'before': [a['name'] for a in before],
              'cached_accounts': [a['name'] for a in cached]}

    if not reboot:
        return dict(detail, ok=True, changed=True, need_reboot=True,
                    message='账号库已清空。系统账号服务还揣着内存缓存，'
                            '重启模拟器后才会真正消失（游戏下次启动要重新登录）。')

    back, how, secs = _restart(timeout=wait, soft=soft)
    if not back:
        return dict(detail, ok=False, need_manual_reboot=True,
                    error='账号库已清空，但模拟器 %d 秒内没恢复 adb 连接。'
                          '请手动重启模拟器 —— 重启后账号即消失，不需要再点一次退出。' % wait)

    # 重启后 adb 掉了 root，重新提权只是为了能再查库；读 dumpsys 不需要 root
    ensure_root()
    after = huawei_accounts()
    if after:
        return dict(detail, ok=False, need_manual_reboot=True, accounts=after,
                    error='重启后系统里仍有华为账号（%s）。'
                          '账号被华为服务自动恢复了，请在模拟器「设置 → 账号」里手动移除。'
                          % ', '.join(a['name'] for a in after))

    return dict(detail, ok=True, changed=True, accounts=[], restart=how,
                elapsed=secs, message='已退出华为账号，游戏下次启动会要求重新登录。')


def _adb_alive():
    """adb 通、且系统已开机完成。"""
    if not SER[0]:
        return False
    try:
        rc, out = run([ADB[0], '-s', SER[0], 'shell', 'getprop sys.boot_completed'], 15)
    except Exception:
        return False
    return rc == 0 and (out or '').strip() == '1'


def _reconnect():
    """重启后 adb 常停在 offline，必须主动断/连一次才会恢复。

    这是「退出账号一直失败」的第二个坑：旧代码只循环 getprop，
    adb 永远回 offline，就永远等不到开机完成，最后报超时。
    """
    if not SER[0]:
        return
    target = SER[0]
    try:
        if ':' in target:
            run([ADB[0], 'disconnect', target], 10)
            run([ADB[0], 'connect', target], 15)
        else:
            run([ADB[0], 'wait-for-device'], 20)
    except Exception:
        pass


def _wait_boot(timeout=300):
    t0 = time.time()
    time.sleep(3)
    n = 0
    while time.time() - t0 < timeout:
        if _adb_alive():
            time.sleep(2)
            return True
        n += 1
        if n % 2 == 0:
            _reconnect()
        time.sleep(3)
    return False


def _restart(timeout=300, soft=True):
    """重启模拟器并等它真正可用，返回 (是否回来, 方式, 秒数)。

    先试软重启（setprop ctl.restart zygote）：实测 adb 只掉 ~20 秒、
    约 1 分钟可用，比 adb reboot 快一倍；不行再整机重启兜底。
    """
    t0 = time.time()
    if soft:
        sh('setprop ctl.restart zygote', t=20)
        # 先等 adb 真的掉线再等它回来，否则会读到重启前的旧 boot_completed=1
        time.sleep(8)
        if _wait_boot(timeout=min(160, max(60, timeout))):
            return True, 'soft', int(time.time() - t0)
    try:
        adb('reboot', t=20)
    except Exception:
        pass
    left = max(60, timeout - int(time.time() - t0))
    return _wait_boot(timeout=left), 'full', int(time.time() - t0)


# ---------------------------------------------------------------- 装 HMS / 修 ABI
def find_hms_apk(explicit=None):
    for p in ([explicit] if explicit else []) + HMS_APK_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    return None


def ensure_hms(apk=None):
    """华为渠道服没 HMS Core 会卡在 BridgeActivity 死循环，必须装上。"""
    if pkg_installed(HMS_PKG):
        return {'ok': True, 'installed': True, 'message': 'HMS Core 已安装'}
    apk = find_hms_apk(apk)
    if not apk:
        return {'ok': False, 'installed': False,
                'error': '没找到 HMS Core 安装包，请把 hmscore.apk 放到 tools/apk/ 目录'}
    rc, out = adb('install', '-r', '-t', apk, t=300)
    if 'Success' in out:
        return {'ok': True, 'installed': True, 'message': 'HMS Core 安装完成'}
    return {'ok': False, 'installed': False, 'error': out[-300:]}


def installed_apk(pkg=HW_PKG):
    """从模拟器里读出已安装 APK 的真实路径（pm path），用于免手输重装。

    MuMu/雷电上 base.apk 对 shell 可读（实测 -rw-r--r-- system:system），
    所以 ARM 转译重装不必让玩家再去找当初的安装包。
    """
    out = sh('pm path %s' % pkg)
    m = re.search(r'package:(.+\.apk)', out)
    return m.group(1).strip() if m else ''


def ensure_abi(apk=None):
    """阿里聚安全的 x86 加固库在模拟器上必崩，逼它走 armeabi-v7a + houdini 转译。

    apk 传外部路径则用它；不传时自动从模拟器 pm path 抠已装 APK，
    避免「修 ABI 必须手输 APK」导致修复分支变成没人能走的死代码。
    """
    abi = primary_abi(HW_PKG)
    if not abi:
        return {'ok': False, 'error': '读不到 primaryCpuAbi（游戏没装？）'}
    if 'arm' in abi.lower():
        return {'ok': True, 'abi': abi, 'changed': False}

    # 1) 优先用玩家显式给的 APK（外部文件）
    if apk and os.path.isfile(apk):
        rc, out = adb('install', '-r', '-t', '--abi', 'armeabi-v7a', apk, t=420)
        src = os.path.basename(apk)
    else:
        # 2) 自动路径：pm path 拿到已装 base.apk，先复制到 /data/local/tmp 再覆盖安装
        #    （报告实测的有效路径就是 /data/local/tmp/*.apk，避免直接装 /data/app 下被占用的文件）
        in_dev = installed_apk(HW_PKG)
        if not in_dev:
            return {'ok': False, 'abi': abi, 'changed': False,
                    'error': '当前是 %s，需要以 armeabi-v7a 重装，但模拟器里也找不到游戏 APK'
                             '（游戏没装或 pm path 失败）。可手动指定 --game-apk。' % abi}
        tmp_apk = '/data/local/tmp/lxqw_hw_abi.apk'
        sh('cp %s %s' % (in_dev, tmp_apk), t=180)
        out = sh('pm install -r -t --abi armeabi-v7a %s' % tmp_apk, t=420)
        sh('rm -f %s' % tmp_apk)
        src = '模拟器内已装 APK（pm path 自动提取）'

    if 'Success' in out:
        return {'ok': True, 'abi': primary_abi(HW_PKG), 'changed': True, 'source': src}
    return {'ok': False, 'abi': abi, 'changed': False,
            'error': (out or '重装无输出')[-300:]}


# ---------------------------------------------------------------- 启动过页
def _screen_png():
    """截【游戏所在显示】的 PNG 字节；失败返回空。"""
    if disp:
        disp.setup(ADB[0], SER[0], HW_PKG)
        return disp.screencap()
    try:
        p = subprocess.run([ADB[0], '-s', SER[0], 'exec-out', 'screencap', '-p'],
                           capture_output=True, timeout=40,
                           creationflags=CREATE_NO_WINDOW)
        data = p.stdout or b''
        return data if data[:8] == b'\x89PNG\r\n\x1a\n' else b''
    except Exception:
        return b''


def on_splash():
    """当前是否停在「启动页」。

    启动页整屏米白（亮像素占九成），游戏主界面是深色草地/池塘（不到一成）。
    用亮度占比判断，比读控件树可靠——启动页是自绘 UI，uiautomator 读不到任何文字。
    解码走 pnglite（纯标准库），绿色包的内嵌 Python 没 PIL 也能判断。
    返回 True=启动页 / False=已在游戏 / None=判断不了（截屏失败）。
    """
    png = _screen_png()
    if not png:
        return None
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import pnglite
        hi = pnglite.hi_ratio(png)
        if hi is not None:
            return hi > 0.5
    except Exception:
        pass
    try:
        from PIL import Image
        import io as _io
        im = Image.open(_io.BytesIO(png)).convert('RGB')
        im.thumbnail((60, 100))
        px = im.load()
        w, h = im.size
        vals = []
        for x in range(w):
            for y in range(h):
                r, g, b = px[x, y]
                vals.append(0.299 * r + 0.587 * g + 0.114 * b)
        if not vals:
            return None
        return (sum(1 for v in vals if v > 200) / float(len(vals))) > 0.5
    except Exception:
        return None


def launch(pkg=HW_PKG, timeout=180):
    """启动游戏并自动越过协议/声明页，尽量把游戏带到主界面。

    返回 state：
      in_game      已在游戏内（进程活着且不再停在协议/登录页）
      need_login   卡在华为账号登录页 —— 必须由用户手动输入账号密码（页面有 FLAG_SECURE，截不了图）
      need_hms     缺 HMS Core
      timeout      超时
    """
    if not SER[0]:
        return {'ok': False, 'state': 'no_device', 'error': '没连上模拟器'}
    if not pkg_installed(pkg):
        return {'ok': False, 'state': 'no_package', 'error': '华为渠道服未安装 %s' % pkg}
    if not pkg_installed(HMS_PKG):
        return {'ok': False, 'state': 'need_hms',
                'error': '缺 HMS Core，华为渠道服无法启动'}

    sh('am force-stop %s' % pkg)
    time.sleep(1)
    sh('logcat -c', t=20)
    sh('am start -n %s/com.Aligames.lxqw.MainActivity' % pkg, t=40)
    if disp:
        disp.setup(ADB[0], SER[0], pkg)
        print('   [hw] 显示检测：%s' % disp.info())
    print('   [hw] 游戏已启动，等启动页加载…')

    t0 = time.time()

    def _login_page(focus):
        low = (focus or '').lower()
        return ('hwid' in low) or ('stub' in low) or ('guide' in low)

    def _done(state, **kw):
        d = {'state': state, 'outcome': state, 'focus': focus_activity(),
             'account': (huawei_accounts() or [{}])[0].get('name', '')}
        d.update(kw)
        d['ok'] = (state == 'in_game')
        return d

    # -------- 阶段 0：等启动页（整屏米白）出现，最多 30 秒 --------
    splash_seen = False
    while time.time() - t0 < min(30, timeout):
        time.sleep(3)
        focus = focus_activity()
        if _login_page(focus):
            if not huawei_accounts():
                return {'ok': False, 'state': 'need_login', 'focus': focus,
                        'message': '需要手动输入华为账号密码（该页面防截屏，程序无法代填）'}
            tap(TAP_LOGIN_AGREE)
            time.sleep(2)
            continue
        on = on_splash()
        if on is True:
            splash_seen = True
            print('   [hw] 已到启动页')
            break
        if on is False:
            print('   [hw] 画面已是游戏主界面')
            return _done('in_game')

    if not splash_seen:
        print('   [hw] 没认出启动页（可能加载慢），继续尝试点击')

    # -------- 阶段 1：勾协议 + 点进入（勾选只做一次，避免把已勾的点回去）--------
    print('   [hw] 勾选协议 → 点击进入游戏')
    tap(TAP_AGREE_CHECKBOX)
    time.sleep(1.5)
    tap(TAP_ENTER_GAME)
    time.sleep(5)

    # -------- 阶段 2：只点「进入游戏」，直到画面不再是启动页 --------
    for i in range(1, 13):
        if time.time() - t0 > timeout:
            break
        on = on_splash()
        if on is False:
            print('   [hw] 已进入游戏')
            return _done('in_game')
        if on is None and i >= 3:
            rc, out = run([ADB[0], '-s', SER[0], 'logcat', '-d'], 30)
            if 'channel login success' in out:
                print('   [hw] 日志显示登录成功，按进入成功处理')
                return _done('in_game')
        focus = focus_activity()
        if _login_page(focus) and not huawei_accounts():
            return {'ok': False, 'state': 'need_login', 'focus': focus,
                    'message': '需要手动输入华为账号密码（该页面防截屏，程序无法代填）'}
        # 每 4 轮补一次勾选：万一第一次点击时页面还没就绪
        if i % 4 == 0:
            tap(TAP_AGREE_CHECKBOX)
            time.sleep(1.5)
        print('   [hw] 第 %d 次点「进入游戏」' % i)
        tap(TAP_ENTER_GAME)
        time.sleep(4)

    rc, out = run([ADB[0], '-s', SER[0], 'logcat', '-d'], 30)
    if '10112' in out or '5002007' in out:
        return {'ok': False, 'state': 'register_closed', 'focus': focus_activity(),
                'message': '该账号在本渠道没有历史角色，且服务器已停止新用户注册'}
    return {'ok': False, 'state': 'stuck', 'focus': focus_activity(),
            'message': '等待超时（%.0fs），游戏仍停在启动页' % (time.time() - t0)}


# ---------------------------------------------------------------- status
def status():
    if not SER[0]:
        return {'ok': False, 'error': '没连上模拟器'}
    hw_ok = pkg_installed(HW_PKG)
    hms_ok = pkg_installed(HMS_PKG)
    accts = huawei_accounts()
    return {
        'ok': True,
        'serial': SER[0],
        'pkg': HW_PKG,
        'pkg_installed': hw_ok,
        'hms_installed': hms_ok,
        'hms_apk': find_hms_apk() or '',
        'abi': primary_abi(HW_PKG) if hw_ok else '',
        'account': accts[0]['name'] if accts else '',
        'accounts': accts,
        'root': 'uid=0' in sh('id'),
        'screen': '%dx%d' % screen_size(),
    }


def main():
    ap = argparse.ArgumentParser(description='华为渠道服辅助（查询 / 换号 / 准备 / 启动）')
    ap.add_argument('cmd', choices=['status', 'logout', 'prepare', 'launch'])
    ap.add_argument('--port', help='模拟器 adb 端口')
    ap.add_argument('--pkg', default=HW_PKG, help='包名（默认华为渠道服）')
    ap.add_argument('--apk', help='HMS Core 安装包路径（prepare 用，默认 tools/apk/hmscore.apk）')
    ap.add_argument('--game-apk', help='游戏 APK 路径（修 ABI 需要；不传则只做检查）')
    ap.add_argument('--no-reboot', action='store_true', help='退出账号后不重启模拟器')
    ap.add_argument('--no-soft', action='store_true',
                    help='退出账号后直接整机重启（默认先软重启 zygote，更快）')
    ap.add_argument('--wait', type=int, default=300,
                    help='退出账号后等模拟器恢复的秒数（默认 300）')
    ap.add_argument('--timeout', type=int, default=180, help='launch 等待秒数')
    a = ap.parse_args()

    if not connect(a.port):
        print(json.dumps({'ok': False, 'error': '连不上模拟器，请先启动 MuMu / 雷电'},
                         ensure_ascii=False))
        return 2

    if a.cmd == 'status':
        res = status()
    elif a.cmd == 'logout':
        res = logout(reboot=not a.no_reboot, wait=a.wait, soft=not a.no_soft)
    elif a.cmd == 'prepare':
        r1 = ensure_hms(a.apk)
        r2 = ensure_abi(a.game_apk)
        res = {'ok': bool(r1.get('ok')) and bool(r2.get('ok')), 'hms': r1, 'abi': r2}
    else:
        res = launch(pkg=a.pkg, timeout=a.timeout)

    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
