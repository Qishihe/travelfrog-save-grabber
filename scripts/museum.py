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

# 游戏窗口可能不在默认显示（MuMu KeepAlive 会开虚拟 display）：
# 截屏 / 点击 / 焦点判断统一走 game_display，避免"看默认屏、点默认屏"误判黑屏。
try:
    import game_display as disp
except Exception:
    disp = None

if disp is None:  # 老目录只替换了 museum.py 时也能按原来的默认屏继续跑
    class _DefaultDisplay(object):
        def setup(self, *args):
            pass

        def info(self):
            return '默认显示 display 0（兼容模式）'

        def screencap(self):
            if not ADB[0] or not SER[0]:
                return b''
            try:
                p = subprocess.run([ADB[0], '-s', SER[0], 'exec-out',
                                    'screencap', '-p'], capture_output=True, timeout=40)
                data = p.stdout or b''
                return data if data[:8] == b'\x89PNG\r\n\x1a\n' else b''
            except Exception:
                return b''

        def tap_base(self, x, y):
            _sh('input tap %d %d' % (int(x), int(y)))
            return True

        def focus(self):
            return _sh('dumpsys window 2>/dev/null | grep -m1 -i mCurrentFocus')

        def is_top(self, pkg):
            return bool(_sh('pidof %s' % pkg)) and (
                pkg.lower() in _sh('dumpsys window | grep -i mCurrentFocus').lower())

    disp = _DefaultDisplay()

# ---- 坐标（900x1600 基准，按真实分辨率自动缩放）----
TAP_MENU = (790, 65)       # 右上「菜单」（三叶草条右侧）
TAP_STORAGE = (700, 65)    # 展开后的「小仓库」
TAP_BLANK = (450, 1450)    # 空白处（草地/地面），用来关掉遗留弹窗
# 启动页「点击进入游戏」——不同渠道服位置不同：
#   官服/米服/华为：y≈1143（底部）
#   b 站服：        y≈960 （青蛙下方，实测点 1143 落在空白处进不去）
# _recover_splash 会按顺序都点一遍，命中哪个算哪个。
TAP_ENTER = (450, 1143)
TAP_ENTER_CANDIDATES = [(450, 960), (450, 1143)]
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


def setup(adb_bin, serial, pkg=None):
    """注入 adb 路径与设备 serial（由 grab_official 调用）。

    pkg 不为空时顺带把游戏显示绑定给 game_display——KeepAlive 虚拟显示必须
    按窗口现查，不能继续截默认屏 / 点默认屏。
    """
    ADB[0] = adb_bin
    SER[0] = serial
    _SIZE[0] = None          # 设备/显示可能变了，尺寸缓存必须作废
    disp.setup(adb_bin, serial, pkg)


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
_SIZE_SRC = ['']
_SIZE_PRINTED = [False]


def _png_size(png):
    """从 PNG IHDR 直接读宽高（不需要 PIL，也不解码整张图）。"""
    if not png or len(png) < 24 or png[:8] != b'\x89PNG\r\n\x1a\n':
        return None
    w = int.from_bytes(png[16:20], 'big')
    h = int.from_bytes(png[20:24], 'big')
    return (w, h) if 100 <= w <= 5000 and 100 <= h <= 5000 else None


def _size():
    """游戏所在【显示】的分辨率，缓存一次。

    ⚠ 米服点偏的根因就在这里：以前用 `wm size` 读的是默认屏（display 0），
    而截图走 `screencap -d <物理id>`、点击走 `input -d <逻辑id>`，拿到的是
    另一块显示。两块屏分辨率不同时，900x1600 → 物理像素的缩放比例就是错的，
    表现出来就是「点得到按钮附近、但总差一截」，而且换个模拟器又不一样。

    现在从游戏显示的截屏 PNG 头取尺寸：与 input -d 派发的是同一块屏，
    基准天然一致。只有完全截不到图时才退回 wm size，并标出来源方便排查。
    """
    if _SIZE[0] is None:
        sz = _png_size(_screen_png())
        if sz:
            _SIZE[0], _SIZE_SRC[0] = sz, '游戏显示截屏'
        else:
            m = re.search(r'(\d+)x(\d+)', _sh('wm size'))
            _SIZE[0] = (int(m.group(1)), int(m.group(2))) if m else (900, 1600)
            _SIZE_SRC[0] = 'wm size 兜底（截屏失败）'
        if not _SIZE_PRINTED[0]:
            _SIZE_PRINTED[0] = True
            print('   博物馆坐标基准：%dx%d（来源：%s）'
                  % (_SIZE[0][0], _SIZE[0][1], _SIZE_SRC[0]))
    return _SIZE[0]


def _screen_png():
    """截【游戏所在显示】的 PNG 字节；失败返回空。

    以前的 exec-out screencap -p 永远截默认屏：MuMu KeepAlive 下默认屏是桌面，
    另一块副屏还可能是全黑，正好命中下面的黑屏判据。现在交给 game_display
    按游戏窗口定位物理 display，找不到时自动退回默认屏。
    """
    return disp.screencap()


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


def _museum_by_stats(st):
    """米黄纸张 + 左右边缘也是米黄，弹窗型页面边缘一律是游戏场景。"""
    return bool(st and st['beige'] >= MUSEUM_BEIGE
                and st['edge_beige'] >= MUSEUM_EDGE)


def on_museum_page():
    """当前是否停在博物馆列表页。True/False，判断不了返回 None。

    只看亮度会把「启动页」（0.88）误判成博物馆页（0.66）；
    博物馆页是全屏米黄纸张，连左右边缘都是米黄，弹窗类页面边缘一律是游戏场景——
    用 beige + edge_beige 双判据才分得开。
    """
    st = screen_stats()
    return None if st is None else _museum_by_stats(st)


def _find_museum_cells(limit=3):
    """在小仓库弹窗里按颜色定位「博物馆」红牌坊图标，返回【多个】候选。

    博物馆那格画的是红牌坊（两根红柱子 + 红屋檐），是弹窗里最密的砖红色块。
    弹窗里另外三处红色都比较好排除：右下「公告」的感叹号（贴屏幕右缘）、
    「礼品盒」的小红盒（红色像素只有牌坊的零头）、标题栏以外的场景杂色。

    以前只取最密的一个簇就点，米服上图标配色/尺寸稍有不同就会认错，而且
    错了以后每轮重试还是点同一个错位置。现在返回前 limit 个互不相邻的
    红色簇，让调用方「点了验证、不对换下一个」。

    坐标按 900x1600 基准返回，缩放所需的真实尺寸由 _size() 从同一张截屏
    取得，保证识别与点击用同一把尺子。空列表 = 没认出来。
    """
    png = _screen_png()
    if not png:
        return []
    sz = _png_size(png)
    try:
        import pnglite
        w, h, bpp, px = pnglite.decode(png)
    except Exception:
        return []
    if bpp < 3 or not sz:
        return []
    w, h = sz                 # 以 PNG 头为准，和 _tap 的缩放严格一致
    if _SIZE[0] != sz:
        # 显示被 KeepAlive 换掉了：后面所有点击都以这张图为准重新缩放
        _SIZE[0], _SIZE_SRC[0] = sz, '游戏显示截屏'
        if not _SIZE_PRINTED[0]:
            _SIZE_PRINTED[0] = True
            print('   博物馆坐标基准：%dx%d（来源：%s）' % (w, h, _SIZE_SRC[0]))

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
        return []

    bins = sorted(grid.items(), key=lambda kv: -kv[1])
    picked = []
    for (bx, by), c in bins:
        if c < 30:
            break
        # 同一个红色簇的相邻 bin 跳过，避免把牌坊的柱子当成三个格子
        if any(abs(bx - p[0]) <= 2 and abs(by - p[1]) <= 2 for p in picked):
            continue
        sx = sy = n = 0
        for (gx, gy), cnt in grid.items():
            if abs(gx - bx) <= 1 and abs(gy - by) <= 1:
                sx += gx * bs * cnt
                sy += gy * bs * cnt
                n += cnt
        if n < (150 if not picked else 80):   # 红得太少，多半是礼品盒/杂色
            continue
        picked.append((bx, by))
        cx = sx / float(n) + bs * 0.5
        cy = sy / float(n) + bs * 0.5
        picked[-1] = (bx, by, int(cx * 900 / w), int(cy * 1600 / h))
        if len(picked) >= limit:
            break
    return [(x, y) for _, _, x, y in picked]


def _find_museum_cell():
    """兼容旧调用：只返回最可信的一个候选（没有则 None）。"""
    cells = _find_museum_cells()
    return cells[0] if cells else None


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
    """按 900x1600 基准坐标点按，自动缩放并在【游戏所在显示】上派发。"""
    w, h = _size()
    disp.tap_base(int(xy[0] * w / 900), int(xy[1] * h / 1600))
    time.sleep(wait)


def _dismiss_popups():
    """点空白处关掉遗留弹窗（回归奖励「收到物品」/分享求助力等）。
    这些弹窗会吃掉后续点击，是博物馆路径失败的头号原因。"""
    for _ in range(2):
        _tap(TAP_BLANK, 0.8)


def _focus():
    """跨显示读焦点：默认屏第一行常是 null，不能只看它。"""
    return disp.focus()


def ensure_foreground(pkg, wait=18):
    """确保游戏在前台且不是黑屏。

    两个必须避开的坑：
      1) MuMu KeepAlive 把游戏放到虚拟 display，默认屏的 mCurrentFocus 是 null、
         截图是桌面/黑副屏；只看默认屏会误判「不在前台（黑屏）」并疯狂 am start，
         在虚拟显示里叠出多个 MainActivity，最后界面假死。
      2) 华为渠道服走 ARM 转译 + 加固壳，偶尔确实会黑屏/掉回桌面。

    所以：前台判断用进程 + topResumedActivity/焦点（game_display.is_top），
    只在真的掉出前台时拉起一次；截屏也走游戏所在的显示。
    """
    disp.setup(ADB[0], SER[0], pkg)
    st = screen_stats()
    black = bool(st and st['brightness'] < 0.05 and st['dark'] > 0.70)
    if disp.is_top(pkg) and not black:
        return True
    print('   [!] 游戏不在前台%s，重新拉起…' % ('（黑屏）' if black else ''))
    _sh('am start -n %s/com.Aligames.lxqw.MainActivity' % pkg, t=40)
    t0 = time.time()
    while time.time() - t0 < wait:
        time.sleep(3)
        if not disp.is_top(pkg):
            continue
        st = screen_stats()
        if st and not (st['brightness'] < 0.05 and st['dark'] > 0.70):
            time.sleep(1)
            return True
    # 进程还在就别再拉第二次（重复 am start 是界面假死的直接原因）。
    return disp.is_top(pkg)


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
        # 各渠道服「进入游戏」按钮位置不同，逐个点；点完若已离开启动页就别再点下一个
        for xy in TAP_ENTER_CANDIDATES:
            _tap(xy, wait)
            st2 = screen_stats()
            if st2 and not (st2['brightness'] > SPLASH_BRIGHT and st2['beige'] < SPLASH_BEIGE):
                break
    st = screen_stats()
    return bool(st) and not (st['brightness'] > SPLASH_BRIGHT and st['beige'] < SPLASH_BEIGE)


def _is_storage_popup(st=None):
    """当前是否停在小仓库九宫格弹窗。

    官服实测 beige 0.28~0.36，米服弹窗底色可能更深/更浅，写死区间会导致
    「弹窗其实开了，却以为没开」然后反复点菜单把它关掉。所以加宽松档：
    只要不是主场景（几乎无米黄）、也不是全屏米黄的博物馆页，就算弹窗。
    """
    st = st if st is not None else screen_stats()
    if not st:
        return False
    if STORAGE_BEIGE[0] <= st['beige'] <= STORAGE_BEIGE[1]:
        return True
    return 0.14 <= st['beige'] < MUSEUM_BEIGE and st['edge_beige'] < 0.25


def _wait_museum(seconds=6.0):
    """点完格子等页面真正切过去（模拟器 ARM 转译比真机慢，别只睡固定 3 秒）。

    米服的纸张颜色可能达不到官服标定的阈值：若页面已经是「大面积米黄 +
    边缘米黄」的接近值，也当打开处理，最终以协议里有没有 museum_list 为准。
    宁可多等一轮协议，也不能把已经打开的页面又点走。
    """
    t0 = time.time()
    soft = False
    while time.time() - t0 < seconds:
        st = screen_stats()
        if st is not None:
            if _museum_by_stats(st):
                return True
            if st['beige'] >= 0.35 and st['edge_beige'] >= 0.25:
                soft = True
        time.sleep(1.2)
    if soft:
        print('   [!] 页面接近博物馆样式，按已打开处理（最终以协议为准）')
        return True
    return False


def _open_storage(tries=2):
    """从主场景打开「菜单 → 小仓库」，已经在弹窗里就不重复点。"""
    for _ in range(tries):
        if _is_storage_popup():
            return True
        _tap(TAP_MENU, 1.6)
        _tap(TAP_STORAGE, 2.0)
        for _ in range(2):
            if _is_storage_popup():
                return True
            _tap(TAP_STORAGE, 1.6)
    return _is_storage_popup()


def _dedup_cells(cells):
    out = []
    for c in cells:
        xy = (int(c[0]), int(c[1]))
        if xy not in out:
            out.append(xy)
    return out


def visit(pkg=None, retries=3):
    """进「小仓库 → 博物馆」触发 museum_load。返回是否成功打开博物馆页。

    模拟器是 ARM 转译跑的，动画比真机慢，菜单展开、九宫格弹窗都要等；
    中途还可能被弹窗吃掉点击、或游戏黑屏掉回桌面。所以每轮都先确认游戏在前台，
    再逐步截图核对，失败整套重来。失败不影响主流程（藏品退回底稿兜底）。
    """
    disp.setup(ADB[0], SER[0], pkg)
    _SIZE[0] = None          # KeepAlive 可能在两轮之间换显示，重新量一次尺子
    if pkg:
        print('   博物馆导航：%s' % disp.info())
    if screen_stats() is None:
        # 截不了图：退回盲点一遍，结果交给「有没有 museum_list」验证
        _size()
        _dismiss_popups()
        for cell in MUSEUM_CELLS:
            _tap(TAP_MENU, 1.2)
            _tap(TAP_STORAGE, 1.6)
            _tap(cell, 2.5)
        print('   [!] 截不了图，已盲点 %d 个候选格（能否抓到看协议）'
              % len(MUSEUM_CELLS))
        return True

    # 官服/米服 4x3，华为 3x3；颜色认不出来时把这些格子挨个试。
    # 米服的失败往往不是"完全点不到"，而是图标配色稍有差异导致认错格，
    # 以前每轮都点同一个错格子，现在错了就换，直到页面真的变了。
    grid = [(x, y) for y in (454, 696, 938) for x in (240, 445, 650)]
    grid += [(x, 1180) for x in (240, 445, 650)]

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

        if not _open_storage():
            st = screen_stats()
            print('   [%d/%d] 小仓库没打开（米黄 %.2f / 亮度 %.2f），重试'
                  % (i, retries, st['beige'] if st else -1,
                     st['brightness'] if st else -1))
            continue

        cells = _find_museum_cells()      # 颜色认出的红牌坊，按可信度排序
        primary = list(MUSEUM_CELLS)
        if pkg and 'huawei' in pkg.lower():
            primary = [MUSEUM_CELLS[1], MUSEUM_CELLS[0]]
        candidates = _dedup_cells(cells + primary + grid)[:10]
        print('   博物馆候选格 %d 个：%s' % (len(candidates), candidates))

        for k, cell in enumerate(candidates):
            # 上一点格进了别的页面就退回场景重开弹窗；没反应则继续点下一格
            if not _is_storage_popup():
                _back_to_scene()
                _dismiss_popups()
                if not _open_storage():
                    break
            _tap(cell, 2.2)
            if _wait_museum(3.6):
                print('   已打开博物馆页（第 %d 轮 / 候选 %d %s）'
                      % (i, k + 1, cell))
                return True
            print('   [!] 候选 %d %s 没进博物馆页，换下一格' % (k + 1, cell))
        print('   [%d/%d] 本轮候选格都没命中，重试...' % (i, retries))
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
