# -*- coding: utf-8 -*-
"""游戏所在的 Android 显示（display）识别与输入/截屏统一入口。

为什么需要它：
    MuMu 12 的「应用保活 / KeepAlive」会把游戏放进独立虚拟显示（实测官服
    display 27）。此时：
      · 游戏窗口、输入焦点、Surface 全在 display 27；
      · 默认屏是 MuMu 桌面，另一块副屏可能是全黑；
      · adb exec-out screencap -p  截的是默认屏 → 误判「游戏不在前台 / 黑屏」；
      · adb shell input tap x y    点的是默认屏 → 点不到「进入游戏」；
      · 反复 am start 会在虚拟显示里叠出多个 MainActivity，界面假死。

关键坑（已实测）：
    input  -d  收【逻辑 display id】：27
    screencap -d 收【物理 display id】：4619827851213736732
    两者不能互换，且编号每次启动可能变，必须现查。

对外接口：
    setup(adb_bin, serial, pkg)  绑定 adb/设备/包名（重复调用很便宜）
    info()                       给日志用的一行显示信息
    refresh(force=False)         重新定位游戏窗口所在显示
    tap_base(x, y) / keyevent()  在【游戏所在显示】上输入
    screencap()                  截【游戏所在显示】的 PNG
    focus() / is_top(pkg)        跨显示读取焦点、判断游戏是否在前台

降级策略：
    · 普通场景（display 0）完全走老命令，不带 -d，行为不变；
    · 找不到游戏窗口 → 回退 display 0；
    · screencap -d / input -d 在老设备不支持 → 自动退回默认显示；
    · 用 pkg 短名（hhb / huawei / bilibili / gamecenter …）兜底匹配窗口，
      避免 dumpsys 把长包名截断时定位不到。
"""
import os
import re
import subprocess
import time

ADB = [None]
SER = [None]
PKG = [None]
LOGICAL = [0]          # input -d 用
PHYSICAL = [None]      # screencap -d 用；display 0 时为 None（保持老行为）

_TS = [0.0]            # 最近一次定位时间，2 秒内复用
_CAP_BAD = set()       # 不支持 screencap -d 的物理 id，只降一次级
_LAST_PRINT = [None]
CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


def setup(adb_bin, serial, pkg=None):
    """绑定 adb / serial / 包名。同一组参数重复调用不会触发重新探测。"""
    changed = (ADB[0] != adb_bin or SER[0] != serial)
    if pkg and PKG[0] != pkg:
        changed = True
        PKG[0] = pkg
    ADB[0] = adb_bin
    SER[0] = serial
    if changed:
        _TS[0] = 0.0
        _CAP_BAD.clear()


def _shell(cmd, t=15):
    if not ADB[0] or not SER[0]:
        return ''
    try:
        p = subprocess.run([ADB[0], '-s', SER[0], 'shell', cmd],
                           capture_output=True, timeout=t,
                           creationflags=CREATE_NO_WINDOW)
        return (p.stdout or b'').decode('utf-8', 'replace').strip()
    except Exception:
        return ''


def _shell_rc(cmd, t=15):
    """返回 (rc, 合并输出)，用于 tap 失败时降级。"""
    if not ADB[0] or not SER[0]:
        return 1, ''
    try:
        p = subprocess.run([ADB[0], '-s', SER[0], 'shell', cmd],
                           capture_output=True, timeout=t,
                           creationflags=CREATE_NO_WINDOW)
        out = (p.stdout or b'').decode('utf-8', 'replace')
        err = (p.stderr or b'').decode('utf-8', 'replace')
        return p.returncode, (out + ('\n' + err if err.strip() else '')).strip()
    except Exception as e:
        return 1, str(e)


def _has_pkg(line, pkg):
    low = (line or '').lower()
    p = (pkg or '').lower()
    if not p:
        return False
    if p in low:
        return True
    # dumpsys 有时把窗口名截短，用最后一段（hhb/huawei/bilibili/gamecenter）兜底。
    short = p.rsplit('.', 1)[-1]
    return (len(short) >= 3 and short in low and
            ('window{' in low or 'activity' in low))


def _display_map():
    """物理 display id 映射：逻辑 id → uniqueId local: 后面的十进制串。"""
    result = {}
    lines = _shell('dumpsys display', 20).splitlines()
    current = None
    for i, line in enumerate(lines):
        u = re.search(r'uniqueId="?local:(\d+)"?', line)
        if u:
            current = u.group(1)
        m = re.search(r'\bmDisplayId=(\d+)', line)
        if not m:
            continue
        did = int(m.group(1))
        if current:
            result[did] = current
        else:
            # 有些 ROM 把 uniqueId 和 mDisplayId 放在不同顺序，前后各看 30 行。
            near = '\n'.join(lines[max(0, i - 30):i + 31])
            u2 = re.search(r'uniqueId="?local:(\d+)"?', near)
            if u2:
                result[did] = u2.group(1)
        current = None

    # 备用来源：dumpsys SurfaceFlinger --display-id
    for line in _shell('dumpsys SurfaceFlinger --display-id', 20).splitlines():
        m = re.search(r'Display\s+(\d+)\b.*?(\d{12,})', line)
        if m:
            result.setdefault(int(m.group(1)), m.group(2))
    return result


def _game_display(pkg):
    """从窗口列表查游戏所在显示，返回 (逻辑 id, 物理 id)。默认 (0, None)。"""
    for cmd in ('dumpsys window windows', 'dumpsys window'):
        for line in _shell(cmd, 25).splitlines():
            if not _has_pkg(line, pkg):
                continue
            m = re.search(r'display#\s*(\d+)', line, re.I)
            if not m:
                continue
            logical = int(m.group(1))
            if logical <= 0:
                return 0, None
            return logical, _display_map().get(logical)
    return 0, None


def refresh(force=False):
    """重新定位游戏窗口。返回 (逻辑 id, 物理 id)。"""
    if not PKG[0]:
        return LOGICAL[0], PHYSICAL[0]
    if not force and time.time() - _TS[0] < 2.0:
        return LOGICAL[0], PHYSICAL[0]
    _TS[0] = time.time()
    logical, phys = _game_display(PKG[0])
    # display 0 故意不带 -d：老设备/普通模拟器行为完全不变。
    new_phys = phys if logical > 0 else None
    if logical != LOGICAL[0] or new_phys != PHYSICAL[0]:
        LOGICAL[0] = logical
        PHYSICAL[0] = new_phys
        _CAP_BAD.clear()
        key = (logical, new_phys)
        if key != _LAST_PRINT[0]:
            _LAST_PRINT[0] = key
            tail = ('，物理 id %s' % new_phys) if new_phys else ''
            print('   [显示] 已定位游戏窗口：逻辑 display %d%s' % (logical, tail))
    return LOGICAL[0], PHYSICAL[0]


def info():
    """给抓取日志用的一行显示信息。"""
    refresh(True)
    if LOGICAL[0] <= 0:
        return '默认显示 display 0'
    return '虚拟显示 display %d（物理 id %s）' % (LOGICAL[0], PHYSICAL[0])


def _fallback_default():
    LOGICAL[0] = 0
    PHYSICAL[0] = None
    _TS[0] = 0.0
    _CAP_BAD.clear()


def _cmd_ok(rc, out):
    low = (out or '').lower()
    return rc == 0 and 'unknown command' not in low and 'error:' not in low


def tap_base(x, y):
    """在【游戏所在显示】上点绝对像素坐标，返回是否成功派发。"""
    refresh()
    x, y = int(x), int(y)
    if LOGICAL[0] > 0:
        rc, out = _shell_rc('input -d %d tap %d %d' % (LOGICAL[0], x, y))
        if _cmd_ok(rc, out):
            return True
        # 虚拟显示可能刚被 KeepAlive 回收；退回默认屏再点一次。
        _fallback_default()
    rc, out = _shell_rc('input tap %d %d' % (x, y))
    return _cmd_ok(rc, out)


def keyevent(code):
    """在【游戏所在显示】上发送按键（返回键等）。"""
    refresh()
    if LOGICAL[0] > 0:
        rc, out = _shell_rc('input -d %d keyevent %d' % (LOGICAL[0], int(code)))
        if _cmd_ok(rc, out):
            return True
        _fallback_default()
    rc, out = _shell_rc('input keyevent %d' % int(code))
    return _cmd_ok(rc, out)


def screencap():
    """截【游戏所在显示】的 PNG 字节；失败返回空（自动降级到默认屏）。"""
    if not ADB[0] or not SER[0]:
        return b''
    refresh()
    phys = PHYSICAL[0]
    args = [ADB[0], '-s', SER[0], 'exec-out', 'screencap']
    if phys and phys not in _CAP_BAD:
        args += ['-d', str(phys)]
    args.append('-p')
    try:
        p = subprocess.run(args, capture_output=True, timeout=40,
                           creationflags=CREATE_NO_WINDOW)
        data = p.stdout or b''
    except Exception:
        data = b''
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return data
    if phys and phys not in _CAP_BAD:
        # 老设备没有 screencap -d：记下来，下次直接走默认屏，不再反复试错。
        _CAP_BAD.add(phys)
        return screencap()
    return b''


def focus():
    """跨显示读取焦点窗口；跳过首行 null，取最后一条有效 mCurrentFocus。"""
    out = _shell('dumpsys window | grep -i mCurrentFocus', 20)
    hits = re.findall(r'mCurrentFocus=\w+\s+([^\s}]+)', out)
    if not hits:
        hits = re.findall(r'u0\s+([^\s}]+)', out)
    for hit in reversed(hits):
        h = hit.strip()
        if h and h.lower() not in ('null', 'nan'):
            return h
    return hits[0] if hits else ''


def is_top(pkg):
    """游戏进程活着 + topResumedActivity/焦点/窗口任一命中，就认为在前台。

    不再只看默认屏的 mCurrentFocus 第一行（KeepAlive 场景下它固定是 null）。
    """
    if not pkg:
        return False
    if not _shell('pidof %s' % pkg, 10).strip():
        return False
    acts = _shell("dumpsys activity activities | grep -i -m5 -E "
                  "'topResumedActivity|ResumedActivity'", 25)
    if _has_pkg(acts, pkg):
        return True
    if _has_pkg(focus(), pkg):
        return True
    for line in _shell('dumpsys window windows', 25).splitlines():
        if _has_pkg(line, pkg) and re.search(r'(?i)state:\s*(resumed|visible)', line):
            return True
    return False
