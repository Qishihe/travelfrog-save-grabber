"""博物馆（账号真实点亮的藏品）—— 进游戏内博物馆页，触发服务器下发 museum_load。

为什么单独成模块：
  1) scripts/grab_official.py 被人工整文件覆盖过两次，把这段逻辑内联在里面会跟着丢；
     抽出来后 grab_official 里只剩一个三行的代理函数，补丁脚本好维护、也好恢复。
  2) 这段导航全靠「截图判据 + 重试」，和抓包主流程耦合度低，单独放更好调试。

调用方（grab_official.py）：
    import museum
    museum.setup(ADB[0], SER[0])
    ok  = museum.visit(pkg)                    # 是否成功打开博物馆页
    got = museum.proto_seen(sessions)          # s2c 里是否已有 museum_list

实测路径（旅行青蛙·中国之旅 1.0.20，基准 900x1600）：
    右上「菜单」(790,65) → 展开 工具栏/物品栏/小仓库/帮助 →「小仓库」(700,65)
    → 九宫格弹窗 → 第 2 行第 3 个「博物馆」(649,696)
旧版脚本写的是 (820,65)/(693,60)/(240,915)：前两下勉强擦到，第三下整个打偏，
博物馆页从没真正打开过，museum_load 自然不下发、藏品只能用底稿兜底。
"""

import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# ---- 坐标（900x1600 基准，按真实分辨率自动缩放）----
TAP_MENU = (790, 65)       # 右上「菜单」（三叶草条右侧）
TAP_STORAGE = (700, 65)    # 展开后的「小仓库」
TAP_MUSEUM = (649, 696)    # 小仓库九宫格里第 2 行第 3 个
TAP_BLANK = (450, 1450)    # 空白处（草地/地面），用来关掉遗留弹窗
TAP_ENTER = (450, 1143)    # 启动页「点击进入游戏」

# ---- 截图判据（pnglite 口径）----
# 实测：博物馆页 beige 0.56 / edge 0.64；小仓库弹窗 beige 0.36 / edge 0；
#       启动页 brightness 0.88 / beige 0.01；庭院 brightness 0.11 / beige 0
MUSEUM_BEIGE = 0.45        # 博物馆页米黄占比下限
MUSEUM_EDGE = 0.40         # 左右边缘也是米黄——弹窗型页面这里恒为 0
STORAGE_BEIGE = (0.28, 0.46)   # 小仓库九宫格弹窗的米黄区间
SPLASH_BRIGHT = 0.80
SPLASH_BEIGE = 0.15

ADB = [None]
SER = [None]


def setup(adb_bin, serial):
    """注入 adb 路径与设备 serial（由 grab_official 调用）。"""
    ADB[0] = adb_bin
    SER[0] = serial


def _sh(cmd, t=90):
    """在模拟器里执行 shell 命令，返回 stdout 文本。"""
    if not ADB[0] or not SER[0]:
        return ''
    try:
        p = subprocess.run([ADB[0], '-s', SER[0], 'shell', cmd],
                           capture_output=True, timeout=t)
        return (p.stdout or b'').decode('utf-8', 'replace').strip()
    except Exception:
        return ''


def _screen_png():
    """截屏 PNG 字节；失败返回空。"""
    if not ADB[0] or not SER[0]:
        return b''
    try:
        p = subprocess.run([ADB[0], '-s', SER[0], 'exec-out', 'screencap', '-p'],
                           capture_output=True, timeout=40)
        data = p.stdout or b''
        return data if data[:8] == b'\x89PNG\r\n\x1a\n' else b''
    except Exception:
        return b''


def screen_stats():
    """页面特征 dict（brightness/beige/edge_beige/dark）；截屏或解码失败返回 None。

    解码用 pnglite（纯标准库）：绿色包的内嵌 Python 没装 PIL，
    这样源码版和绿色包判断口径完全一致，不会一个能用一个不能用。
    """
    png = _screen_png()
    if not png:
        return None
    try:
        import pnglite
        return pnglite.page_stats(png)
    except Exception:
        return None


def on_museum_page():
    """当前是否停在博物馆列表页。True/False，判断不了返回 None。

    只看亮度会把「启动页」（0.88）误判成博物馆页（0.66）；
    博物馆页是全屏米黄纸张，连左右边缘都是米黄，弹窗类页面边缘一律是游戏场景——
    用 beige + edge_beige 双判据才分得开。
    """
    st = screen_stats()
    if st is None:
        return None
    return st['beige'] >= MUSEUM_BEIGE and st['edge_beige'] >= MUSEUM_EDGE


def _tap(xy, wait=1.2):
    """按 900x1600 基准坐标点按，自动按真实分辨率缩放。"""
    w, h = 900, 1600
    m = re.search(r'(\d+)x(\d+)', _sh('wm size'))
    if m:
        w, h = int(m.group(1)), int(m.group(2))
    _sh('input tap %d %d' % (int(xy[0] * w / 900), int(xy[1] * h / 1600)))
    time.sleep(wait)


def _dismiss_popups():
    """点空白处关掉遗留弹窗（回归奖励「收到物品」/分享求助力等）。
    这些弹窗会吃掉后续点击，是博物馆路径失败的头号原因。"""
    for _ in range(2):
        _tap(TAP_BLANK, 0.8)


def _focus():
    return _sh("dumpsys window 2>/dev/null | grep -m1 -i mCurrentFocus")


def ensure_foreground(pkg, wait=18):
    """确保游戏在前台且不是黑屏。

    华为渠道服走 ARM 转译 + 加固壳，偶尔会在启动/切页时黑屏或掉回桌面
    （logcat 里就是一句 Force stopping ... app died, no saved state）。
    掉出去时点哪儿都白点，所以进博物馆前先把游戏拉回前台。
    """
    focus = _focus()
    st = screen_stats()
    black = bool(st and st['brightness'] < 0.05 and st['dark'] > 0.70)
    if pkg.lower() in focus.lower() and not black:
        return True
    print('   [!] 游戏不在前台%s，重新拉起…' % ('（黑屏）' if black else ''))
    _sh('am start -n %s/com.Aligames.lxqw.MainActivity' % pkg, t=40)
    t0 = time.time()
    while time.time() - t0 < wait:
        time.sleep(3)
        st = screen_stats()
        if st and not (st['brightness'] < 0.05 and st['dark'] > 0.70):
            time.sleep(1)
            return True
    return False


def _recover_splash(tries=3, wait=10):
    """卡在启动页/加载页（大面积米白、几乎没米黄）时点「进入游戏」，最多试 tries 轮。

    模拟器负载高时游戏启动会明显变慢，点一次不够——实测慢的时候要连点十几次、
    等一两分钟才真正进主界面，所以这里给足重试和等待。
    返回 True 表示当前已经不在启动页。
    """
    for k in range(tries):
        st = screen_stats()
        if st is None:
            return False
        if not (st['brightness'] > SPLASH_BRIGHT and st['beige'] < SPLASH_BEIGE):
            return True
        if k == 0:
            print('   还在启动页/加载页，点「进入游戏」等它（最多 %d 秒）…' % (tries * wait))
        _tap(TAP_ENTER, wait)
    st = screen_stats()
    return bool(st) and not (st['brightness'] > SPLASH_BRIGHT and st['beige'] < SPLASH_BEIGE)


def visit(pkg=None, retries=3):
    """进「小仓库 → 博物馆」触发 museum_load。返回是否成功打开博物馆页。

    模拟器是 ARM 转译跑的，动画比真机慢，菜单展开、九宫格弹窗都要等；
    中途还可能被弹窗吃掉点击、或游戏黑屏掉回桌面。所以每轮都先确认游戏在前台，
    再逐步截图核对，失败整套重来。失败不影响主流程（藏品退回底稿兜底）。
    """
    if screen_stats() is None:
        # 截不了图：退回盲点一遍，结果交给「有没有 museum_list」验证
        _dismiss_popups()
        _tap(TAP_MENU, 1.8)
        _tap(TAP_STORAGE, 2.2)
        _tap(TAP_MUSEUM, 3.0)
        print('   [!] 截不了图，已盲点一遍（能否抓到看协议）')
        return True

    for i in range(1, retries + 1):
        if pkg and not ensure_foreground(pkg):
            print('   [%d/%d] 游戏拉不回来，跳过博物馆' % (i, retries))
            return False
        # 先确认已经离开启动页/加载页：启动页上点哪儿都会被「进入游戏」热区吃掉
        if not _recover_splash():
            print('   [%d/%d] 游戏还没进主界面（卡在启动页），重试' % (i, retries))
            continue
        _dismiss_popups()
        if on_museum_page():
            print('   已在博物馆页')
            return True

        _tap(TAP_MENU, 1.8)          # 展开菜单
        _tap(TAP_STORAGE, 2.2)       # 小仓库

        # 确认九宫格真的弹出来了，没弹就再点一次（最多两次）
        st = None
        for _ in range(2):
            st = screen_stats()
            if st is None or STORAGE_BEIGE[0] <= st['beige'] <= STORAGE_BEIGE[1]:
                break
            _tap(TAP_STORAGE, 1.8)
        if st is not None and not (STORAGE_BEIGE[0] <= st['beige'] <= STORAGE_BEIGE[1]):
            print('   [%d/%d] 小仓库没打开（米黄 %.2f / 亮度 %.2f），重试'
                  % (i, retries, st['beige'], st['brightness']))
            continue

        _tap(TAP_MUSEUM, 3.0)
        if on_museum_page():
            print('   已打开博物馆页（第 %d 次尝试）' % i)
            return True
        print('   [%d/%d] 没进到博物馆页，重试...' % (i, retries))
    return False


def sessions_since(cap_dir, since):
    """抓包目录里 s2c.bin 在 since 之后还在写的会话。

    按文件 mtime 而不是目录 mtime——目录 mtime 只在增删文件时变，
    续写同一个 s2c.bin 不会更新它。
    """
    out = []
    try:
        names = os.listdir(cap_dir)
    except Exception:
        return out
    for name in names:
        d = os.path.join(cap_dir, name)
        if not os.path.isdir(d):
            continue
        p = os.path.join(d, 's2c.bin')
        try:
            if os.path.isfile(p) and os.path.getmtime(p) >= since:
                out.append(d)
        except Exception:
            pass
    return out


def proto_seen(sessions):
    """s2c.bin 里是否已出现博物馆列表（museum_list 是数组）。

    注意 museumday.load 里也有 museum_list，但那是空对象 {}，不是藏品数组；
    真回包才是 [{...},{...}]，所以必须匹配左方括号。
    """
    pat = re.compile(rb'"museum_list"\s*:\s*\[')
    for d in sessions:
        p = os.path.join(d, 's2c.bin')
        if not os.path.isfile(p):
            continue
        try:
            if pat.search(open(p, 'rb').read()):
                return True
        except Exception:
            pass
    return False
