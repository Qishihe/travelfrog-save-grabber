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
    → 弹窗格子 →「博物馆」

⚠ 格子排列不是所有服都一样，这是踩过的坑：
    官服、米服   4 行 3 列（12 格）→ 博物馆 = 第 3 行第 1 列 ≈ (240,938)
    华为渠道服   3 行 3 列（ 9 格）→ 博物馆 = 第 2 行第 3 列 ≈ (649,696)
早先按 3x3 写死 (649,696)，在官服/米服上正好落在第 2 行第 3 列的「手工品」上——
所以现在不再猜格子：弹窗一开就截屏按颜色找「红牌坊」图标（见 _find_museum_cell），
坐标表只当认不出图标时的兜底，并且每轮重试换一个候选。
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
TAP_BLANK = (450, 1450)    # 空白处（草地/地面），用来关掉遗留弹窗
TAP_ENTER = (450, 1143)    # 启动页「点击进入游戏」
TAP_BACK = (82, 60)        # 二级页面（博物馆/手工品/相册）左上角「返回」
                           # 注意：这游戏的系统返回键是死的，keyevent 4 按了页面不动

# 「博物馆」兜底坐标表：只在截屏认不出图标时按顺序试（见模块 docstring 的坑）。
# 顺序 = 官服/米服(4x3) → 华为渠道服(3x3)。
MUSEUM_CELLS = [(240, 938), (649, 696)]
TAP_MUSEUM = MUSEUM_CELLS[0]   # 兼容旧名字，仅供盲点分支使用

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


_SIZE = [None]


def _size():
    """屏幕真实分辨率，只问一次 adb 然后缓存（_tap 一秒能调好几次，别每次都开进程）。"""
    if _SIZE[0] is None:
        m = re.search(r'(\d+)x(\d+)', _sh('wm size'))
        _SIZE[0] = (int(m.group(1)), int(m.group(2))) if m else (900, 1600)
    return _SIZE[0]


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


def _find_museum_cell():
    """在小仓库弹窗里按颜色定位「博物馆」图标，返回 900x1600 基准坐标。

    博物馆那格画的是红牌坊（两根红柱子 + 红屋檐），是弹窗里最密的砖红色块。
    弹窗里另外三处红色都比较好排除：右下「公告」的感叹号（贴屏幕右缘）、
    「礼品盒」的小红盒（红色像素只有牌坊的零头）、标题栏以外的场景杂色。
    所以这里限定在弹窗主体区域找红色，取最密的簇算质心。

    返回 None 表示没认出来（截屏失败 / 不在小仓库弹窗 / 图标风格变了），
    调用方按 MUSEUM_CELLS 坐标表兜底。
    """
    png = _screen_png()
    if not png:
        return None
    try:
        import pnglite
        w, h, bpp, px = pnglite.decode(png)
    except Exception:
        return None
    if bpp < 3:
        return None

    # 弹窗主体（去掉屏幕右缘的公告按钮、顶部状态栏、底部小屋/商店）
    x0, x1 = int(w * 0.10), int(w * 0.86)
    y0, y1 = int(h * 0.18), int(h * 0.80)
    bs = max(12, int(w * 0.03))
    grid = {}
    for y in range(y0, y1, 2):
        base = y * w * bpp
        for x in range(x0, x1, 2):
            o = base + x * bpp
            r, g, b = px[o], px[o + 1], px[o + 2]
            if r > 120 and (r - g) > 50 and (r - b) > 50 and g < 140:
                k = (x // bs, y // bs)
                grid[k] = grid.get(k, 0) + 1
    if not grid:
        return None

    # 最密的 bin 及其周围 1 圈的 bin 一起算质心
    best = max(grid, key=grid.get)
    sx = sy = n = 0
    for (bx, by), c in grid.items():
        if abs(bx - best[0]) <= 1 and abs(by - best[1]) <= 1:
            sx += bx * bs * c
            sy += by * bs * c
            n += c
    if n < 150:            # 红得太少，多半是礼品盒/杂色，不当真
        return None
    cx = sx / float(n) + bs * 0.5
    cy = sy / float(n) + bs * 0.5
    return (int(cx * 900 / w), int(cy * 1600 / h))


def _back_to_scene(tries=2):
    """把二级页面推回主场景。

    点错格（比如进了「手工品」「相册」）之后，右上角菜单是点不动的，
    不先退回来，后面所有点击都白费——这也是重试能救回来的前提。

    三个坑：
      · 这游戏的系统返回键是死的（keyevent 4 按下去页面纹丝不动），只能点左上角「返回」；
      · 主场景左上角就是金币数字，点上去没有任何反应（实测无弹窗），所以可以无脑点，
        不用先判断"我现在在哪一层"——判断本身才是容易出错的地方；
      · 唯一不能点的是启动页，那儿整屏都是「进入游戏」热区，交给 _recover_splash。
    """
    st = screen_stats()
    if st is not None and st['brightness'] > SPLASH_BRIGHT and st['beige'] < SPLASH_BEIGE:
        return False
    for _ in range(tries):
        _tap(TAP_BACK, 1.4)
    return True


def _tap(xy, wait=1.2):
    """按 900x1600 基准坐标点按，自动按真实分辨率缩放。"""
    w, h = _size()
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
        # 上一轮可能误点进了别的页面（手工品/相册…），先退回主场景
        _back_to_scene()
        _dismiss_popups()
        if on_museum_page():
            print('   已在博物馆页')
            return True

        _tap(TAP_MENU, 1.8)          # 展开菜单
        _tap(TAP_STORAGE, 2.2)       # 小仓库

        # 确认弹窗真的弹出来了，没弹就再点一次（最多两次）
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

        # 认图标 > 猜坐标：格子数官服/米服是 4x3、华为服是 3x3，写死必点错格。
        cell = _find_museum_cell()
        if cell:
            print('   已定位「博物馆」图标 %s' % (cell,))
        else:
            cells = MUSEUM_CELLS
            if pkg and 'huawei' in pkg.lower():
                cells = [MUSEUM_CELLS[1], MUSEUM_CELLS[0]]
            cell = cells[(i - 1) % len(cells)]
            print('   [!] 没认出「博物馆」图标，按坐标表试 %s' % (cell,))

        _tap(cell, 3.0)
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
