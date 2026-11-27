# -*- coding: utf-8 -*-
"""启动页自动过页：勾协议 / 点同意弹窗 / 点「进入游戏」/ 认出登录页。

为什么需要它：
    OPPO、vivo、米服的启动页不只有「进入游戏」一个按钮，前面还挡着
      · 「我已阅读并同意」复选框（没勾上，点「进入游戏」永远没反应）
      · 联运服务声明 / 隐私协议弹窗（「同意」「允许」）
      · 渠道账号授权页（OPPO 游戏中心 / 欢太账号），这层必须人工登录
    旧逻辑只盲点三个「进入游戏」坐标，复选框没勾就是死循环，日志表现是
    「一直点进入游戏、一个字节都抓不到、最后卡在启动页」。

每轮判定顺序：
    1. 前台是渠道账号/授权页 → 返回 login，让上层立刻停下并提示人工登录，
       而不是白等到 150 秒超时；
    2. （默认显示时）dump 控件文本，命中「同意 / 允许 / 进入游戏」就点它，
       WebView 弹窗、原生弹窗都能覆盖；
    3. 读不到文本（自绘启动页）→ 点协议复选框 + 轮换「进入游戏」坐标。

坐标一律用 900x1600 基准，内部按真实分辨率换算后交给调用方的 tap_fn，
调用方只负责「在游戏所在显示上派发一次点击」。
"""
import os
import re
import subprocess

ADB = [None]
SER = [None]
PKG = [None]
SIZE = [None]
CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

REF_W, REF_H = 900, 1600

# 启动页「我已阅读并同意」复选框（华为实测，同款游戏各渠道布局一致）
TAP_AGREE = (141, 1305)
# 「进入游戏 / 点击进入」在不同渠道服、不同分辨率下的实际落点
ENTER_CANDIDATES = [(450, 960), (450, 1143), (450, 1170), (450, 1250)]

# 弹窗上值得点的文字（长的排前面，先命中更精确的那个）
OK_WORDS = ('同意并继续', '同意并登录', '同意并授权', '我已阅读并同意',
            '同意', '允许', '始终允许', '确定', '我知道了', '继续',
            '点击进入', '进入游戏', '开始游戏', '进入', '开始', '跳过')

# 前台 Activity 命中这些 = 渠道账号/授权页，程序代点没用，必须人工登录
LOGIN_HINTS = ('heytap', 'com.oppo.', 'com.coloros.', 'gcsdk', 'com.nearme.gamecenter/',
               'com.nearme.oauth', 'account.ui', 'oauth', 'login.activity',
               'com.vivo.', 'com.miui.account', 'com.xiaomi.account')

# 这些词出现说明已经进游戏了，不用再点
IN_GAME_WORDS = ('庭院', '旅行', '商店', '背包', '相册', '菜单')

# 这些文字出现 = 人停在登录 / 切换账号界面。
# OPPO 这类渠道服的登录页很多是游戏内 WebView，前台包名仍是游戏自己，
# 靠 is_login_page() 认不出来；继续盲点会把「切换账号」点掉、或者点到空白处，
# 表现就是玩家说的「能切换账号，但抓取器直接跳过了」。认出就停手，交给人操作。
LOGIN_TEXT_WORDS = ('切换账号', '切换帐号', '登录其他账号', '其他账号登录',
                    '其他方式登录', '账号登录', '手机号登录', '验证码登录',
                    '使用其他账号')


def setup(adb_bin, serial, pkg=None):
    """绑定 adb / serial / 包名（重复调用很便宜）。"""
    if ADB[0] != adb_bin or SER[0] != serial:
        SIZE[0] = None
    ADB[0] = adb_bin
    SER[0] = serial
    if pkg:
        PKG[0] = pkg


def _shell(cmd, t=20):
    if not ADB[0] or not SER[0]:
        return ''
    try:
        p = subprocess.run([ADB[0], '-s', SER[0], 'shell', cmd],
                           capture_output=True, timeout=t,
                           creationflags=CREATE_NO_WINDOW)
        return (p.stdout or b'').decode('utf-8', 'replace').strip()
    except Exception:
        return ''


def size():
    """真实分辨率，问一次就缓存。"""
    if SIZE[0] is None:
        m = re.search(r'(\d+)\s*x\s*(\d+)', _shell('wm size'))
        SIZE[0] = (int(m.group(1)), int(m.group(2))) if m else (REF_W, REF_H)
    return SIZE[0]


def to_phys(x, y):
    """900x1600 基准 → 真实像素。"""
    w, h = size()
    return int(x * w / float(REF_W)), int(y * h / float(REF_H))


def to_base(x, y):
    """真实像素 → 900x1600 基准（dump 出来的坐标要转换后再交给 tap_fn）。"""
    w, h = size()
    return int(x * float(REF_W) / w), int(y * float(REF_H) / h)


def focus():
    """当前焦点窗口（跳过首行 null）。"""
    out = _shell('dumpsys window | grep -i mCurrentFocus')
    hits = re.findall(r'mCurrentFocus=\w+\s+([^\s}]+)', out)
    if not hits:
        hits = re.findall(r'u0\s+([^\s}]+)', out)
    for h in reversed(hits):
        h = h.strip()
        if h and h.lower() not in ('null', 'nan'):
            return h
    return hits[0] if hits else ''


def is_login_page(pkg=None):
    """前台是不是渠道账号/授权页。

    必须先把游戏自己的包名排除：OPPO 服包名 com.ali.croak.nearme.gamecenter
    本身就含 nearme，不排除会把游戏主界面误判成登录页。
    """
    pkg = (pkg or PKG[0] or '').lower()
    f = (focus() or '').lower()
    if not f:
        return False
    if pkg and pkg in f:
        return False
    return any(k in f for k in LOGIN_HINTS)


def _nodes():
    """dump 当前界面可点文本，返回 [(text, cx, cy)]（真实像素）。

    自绘启动页读不到任何文字，返回空列表，由调用方退回固定坐标。
    """
    _shell('uiautomator dump /sdcard/_boot_ui.xml >/dev/null 2>&1', t=30)
    xml = _shell('cat /sdcard/_boot_ui.xml 2>/dev/null', t=30)
    if not xml or '<?xml' not in xml:
        return []
    out = []
    for m in re.finditer(r'text="([^"]+)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml):
        text = m.group(1).strip()
        if not text:
            continue
        x1, y1, x2, y2 = (int(m.group(i)) for i in range(2, 6))
        out.append((text, (x1 + x2) // 2, (y1 + y2) // 2))
    return out


def tap_text(tap_fn, words=OK_WORDS):
    """按文本点一次可点的按钮，返回点到的文字（没点到返回 ''）。"""
    for text, cx, cy in _nodes():
        low = text.lower()
        if any(w in text for w in IN_GAME_WORDS):
            return '__ingame__'
        if any(w in text for w in LOGIN_TEXT_WORDS):
            return '__login__'
        for w in words:
            if w in text or w.lower() in low:
                bx, by = to_base(cx, cy)
                if tap_fn(bx, by):
                    return text
                return ''
    return ''


def pass_once(pkg, tap_fn, n, virtual=False, log=None, prefer=None):
    """执行一轮过页，返回状态：

    'ingame'  画面已是主界面（读到主界面文字）
    'login'   前台是渠道账号/授权页 → 上层应停止并提示人工登录
    'tapped'  本轮点到了某个按钮（文本命中）
    'agree'   勾了协议复选框 + 点了「进入游戏」
    'none'    什么都没做（没认出页面）
    """
    if is_login_page(pkg):
        return 'login'

    # 虚拟显示（MuMu KeepAlive）下 uiautomator 只能 dump 默认屏，会读到桌面，
    # 所以直接走固定坐标，避免被错误文本带偏。
    if not virtual:
        hit = tap_text(tap_fn)
        if hit == '__ingame__':
            return 'ingame'
        if hit == '__login__':
            if log:
                log('   认出登录 / 切换账号界面，停止自动点击')
            return 'login'
        if hit:
            if log:
                log('   过页：点「%s」' % hit)
            return 'tapped'

    # 第 1、4、7…… 轮补勾一次协议：只勾一次可能赶上页面还没就绪，
    # 但又不能每轮都点，否则本来勾上的会被点回去。
    if n % 3 == 0:
        bx, by = TAP_AGREE
        tap_fn(bx, by)
    bx, by = prefer if (n <= 1 and prefer) else ENTER_CANDIDATES[n % len(ENTER_CANDIDATES)]
    ok = tap_fn(bx, by)
    return 'agree' if ok else 'none'


def describe_login(pkg=None):
    """给日志用：登录页是哪一层，提示用户怎么做。"""
    f = focus() or ''
    low = f.lower()
    pkg = (pkg or PKG[0] or '').lower()
    if pkg and pkg in low:
        return ('停在游戏内登录 / 切换账号界面（前台仍是游戏自己）。'
                '请在模拟器里手动登录或切换账号，进到庭院主界面后再点开始抓取；'
                '抓取器不会替你点，以免把登录页点掉。')
    if 'heytap' in low or 'com.oppo.' in low or 'coloros' in low or 'oauth' in low \
            or 'nearme' in low:
        return ('停在 OPPO 账号/欢太授权页，需要人工登录一次：'
                '在模拟器里点「同意并登录 / 一键登录」，进到游戏主界面后再点开始抓取。')
    if 'vivo' in low:
        return '停在 vivo 账号页，需要人工登录一次后再抓取。'
    if 'xiaomi' in low or 'miui.account' in low:
        return '停在小米账号页，需要人工登录一次后再抓取。'
    if 'hwid' in low or 'stub' in low:
        return '停在华为账号页，需要人工登录一次后再抓取。'
    return '停在渠道账号/授权页（%s），需要人工登录一次后再抓取。' % (f or '未知页面')
