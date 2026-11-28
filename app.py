# -*- coding: utf-8 -*-
"""旅行青蛙 · 官方服/米服 存档抓取器（本地网页界面）

设计要点：
  * 只依赖 Python 标准库（http.server / subprocess / threading），便携运行时即可跑；
  * 只监听 127.0.0.1，端口被占用自动顺延；
  * 真正的抓取逻辑仍由 scripts/grab_official.py 完成，本文件只负责
    「起服务 → 收参数 → 起子进程 → 把 stdout 逐行喂给网页 → 报告结果」。

启动：runtime\\python\\python.exe app.py   （安装后由 启动.vbs 隐窗调用）
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, 'web')
SCRIPTS = os.path.join(ROOT, 'scripts')
GRAB = os.path.join(SCRIPTS, 'grab_official.py')
HW = os.path.join(SCRIPTS, 'huawei_channel.py')
OPPO = os.path.join(SCRIPTS, 'oppo_channel.py')
NET_RESTORE = os.path.join(SCRIPTS, 'net_restore.py')
OUTPUT = os.path.join(ROOT, 'output')
ADB = os.path.join(ROOT, 'tools', 'platform-tools', 'adb.exe')
CA_PEM = os.path.join(ROOT, 'tools', 'mitm', 'ca.pem')

DEFAULT_PORT = 8727
CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
LOCK = threading.Lock()

# 日志标记 -> 进度步骤（0 准备 / 1 代理 / 2 证书 / 3 抓取 / 4 解析 / 5 生成）
STEP_MARKS = [
    (r'\[1/5\]', 1),
    (r'\[2/5\]', 2),
    (r'\[3/5\]', 3),
    (r'\[3\.5/5\]', 3),
    (r'\[4/5\]', 4),
    (r'\[5/5\]', 5),
]

JOB = {'cur': None}


class Job(object):
    def __init__(self, params, cmd):
        self.id = str(int(time.time() * 1000))
        self.params = params
        self.cmd = cmd
        self.lines = []
        self.state = 'running'          # running / done / failed / stopped
        self.rc = None
        self.step = 0
        self.out_dir = None
        self.save_file = None
        self.proc = None
        self.error = ''
        self.started = time.time()


def _push(job, text):
    with LOCK:
        job.lines.append({'t': time.strftime('%H:%M:%S'), 'x': text})


def _scan_step(job, line):
    for pat, st in STEP_MARKS:
        if re.search(pat, line) and st > job.step:
            job.step = st
            break


def _pick_save(dirpath, since=0):
    """挑本次生成的可导入存档：新命名「导入单机版_昵称_旅行青蛙存档_时间」，
    兼容旧命名「…旅行青蛙存档_官服抓取_时间」。排除原始数据明文。"""
    best = None
    try:
        for f in os.listdir(dirpath):
            is_save = f.endswith('.json') and '原始数据' not in f and (
                ('导入单机版' in f and '旅行青蛙存档' in f) or '官服抓取' in f)
            if is_save:
                p = os.path.join(dirpath, f)
                try:
                    mt = os.path.getmtime(p)
                except Exception:
                    mt = 0
                if mt < since:
                    continue
                if best is None or mt > best[0]:
                    best = (mt, p)
    except Exception:
        pass
    return best[1] if best else None


def _scan_output(job, before, since=0):
    """扫 output：认出本次新建的目录（目录名不固定，可能是「一键抓官服_时间戳」也可能是
    「玩家名旅行青蛙存档_官服抓取_时间戳」），并找出这次的存档文件。"""
    try:
        if not os.path.isdir(OUTPUT):
            return
        names = set(os.listdir(OUTPUT))
        new = names - before
        dirs = []
        for n in new:
            p = os.path.join(OUTPUT, n)
            if not os.path.isdir(p):
                continue
            try:
                mt = os.path.getmtime(p)
            except Exception:
                mt = 0
            if mt >= since:
                dirs.append((mt, p))
        if dirs:
            dirs.sort()
            job.out_dir = dirs[-1][1]
        f = _pick_save(job.out_dir, since) if job.out_dir else None
        if not f:
            f = _pick_save(OUTPUT, since)
        if f:
            job.save_file = f
    except Exception:
        pass


def _watch_output(job, before):
    since = job.started - 120
    while job.state == 'running':
        _scan_output(job, before, since)
        time.sleep(0.6)


def _run(job):
    before = set(os.listdir(OUTPUT)) if os.path.isdir(OUTPUT) else set()
    os.makedirs(OUTPUT, exist_ok=True)
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'      # 子进程中文输出全部走 utf-8，避免乱码
    env['PYTHONUNBUFFERED'] = '1'
    threading.Thread(target=_watch_output, args=(job, before), daemon=True).start()
    try:
        p = subprocess.Popen(
            job.cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', bufsize=1, env=env,
            creationflags=CREATE_NO_WINDOW,
        )
        job.proc = p
        assert p.stdout is not None
        for line in iter(p.stdout.readline, ''):
            _push(job, line.rstrip('\r\n'))
            _scan_step(job, line)
        p.wait()
        job.rc = p.returncode
    except Exception as e:
        job.error = str(e)
        job.rc = -1
        _push(job, '[X] 启动抓取进程失败：%s' % e)
    finally:
        if job.state == 'running':
            job.state = 'done' if job.rc == 0 else 'failed'
        job.step = max(job.step, 5) if job.state == 'done' else job.step
        # 收尾再扫一次输出，避免最后几秒的文件漏掉
        _scan_output(job, set(), job.started - 300)


def status_info():
    cur = JOB['cur']
    return {
        'ok': True,
        'root': ROOT,
        'adb': os.path.isfile(ADB),
        'grab': os.path.isfile(GRAB),
        'huawei': os.path.isfile(HW),
        'ca': os.path.isfile(CA_PEM),
        'output': OUTPUT,
        'runtime': sys.executable,
        'running': bool(cur and cur.state == 'running'),
        'version': '2.8',
    }


# ---------------- 模拟器 adb 端口探测 ----------------
# 各家模拟器的 adb 端口：MuMu 12 默认 16384（但会随实例/占用漂移到 16416、16448…），
# 同时也保留老的 7555；雷电 5555 起；夜神 62001 起；逍遥 21503 起。
KNOWN_ADB_PORTS = [
    16384, 16416, 16448, 7555, 7556, 7557, 5555, 5557, 5556,
    62001, 62025, 62026, 21503, 21513,
]
EMU_PROC_KEYWORDS = (
    'mumu', 'nemu', 'ldvbox', 'dnplayer', 'ldplayer', 'ld9', 'nox',
    'hd-player', 'memu', 'bluestacks', 'qemu', 'vboxheadless',
)
DEV_CACHE = {'t': 0.0, 'ports': []}


def _adb_raw(*args, timeout=15):
    """跑一条 adb 命令，只返回 stdout（失败返回空串，不抛）。"""
    try:
        p = subprocess.run([ADB] + list(args), capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        return p.stdout or ''
    except Exception:
        return ''


def _list_devices():
    devs = []
    for ln in (_adb_raw('devices') or '').splitlines():
        ln = ln.strip()
        if not ln or ln.lower().startswith('list of devices'):
            continue
        parts = ln.split()
        if len(parts) >= 2:
            devs.append({'serial': parts[0], 'state': parts[1]})
    return devs


def _listening_by_pid():
    """{pid: [port, ...]}，只取本机 TCP LISTENING。"""
    res = {}
    if os.name != 'nt':
        return res
    try:
        out = subprocess.run(['netstat', '-ano', '-p', 'TCP'], capture_output=True,
                             text=True, encoding='utf-8', errors='replace',
                             timeout=10, creationflags=CREATE_NO_WINDOW).stdout or ''
    except Exception:
        return res
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) < 5 or parts[-2].upper() != 'LISTENING':
            continue
        port = parts[1].rsplit(':', 1)[-1]
        pid = parts[-1]
        if port.isdigit() and pid.isdigit():
            res.setdefault(int(pid), []).append(int(port))
    return res


def _pid_names(pids):
    names = {}
    if not pids or os.name != 'nt':
        return names
    try:
        out = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True,
                             text=True, encoding='utf-8', errors='replace',
                             timeout=10, creationflags=CREATE_NO_WINDOW).stdout or ''
    except Exception:
        return names
    for ln in out.splitlines():
        cols = [c.strip().strip('"') for c in ln.split('","')]
        if len(cols) < 2 or not cols[1].isdigit():
            continue
        pid = int(cols[1])
        if pid in pids:
            names[pid] = cols[0].lower()
    return names


def _emu_ports():
    """从模拟器进程名反查它到底监听哪个端口（MuMu 12 端口会漂移，不能写死）。
    只保留 adb 端口常见段：模拟器还有一堆内部服务端口（7875/14221/23596…），
    拿去 adb connect 会一直卡到超时，能把「重新检测」拖成一分钟。"""
    if os.name != 'nt':
        return []
    now = time.time()
    if now - DEV_CACHE['t'] < 15:          # 15 秒内复用，避免连点「重新检测」反复扫
        return DEV_CACHE['ports']
    ranges = ((5000, 5600), (7555, 7600), (16384, 16600),
              (21500, 21600), (62000, 62100))
    ports = []
    try:
        by_pid = _listening_by_pid()
        names = _pid_names(set(by_pid))
        for pid, plist in by_pid.items():
            nm = names.get(pid, '')
            if not nm or not any(k in nm for k in EMU_PROC_KEYWORDS):
                continue
            for p in plist:
                if p == DEFAULT_PORT or p == 5037 or p in ports:
                    continue
                if any(a <= p <= b for a, b in ranges):
                    ports.append(p)
    except Exception:
        ports = []
    DEV_CACHE.update(t=now, ports=ports)
    return ports


def _tcp_open(port, timeout=0.2):
    """秒级探一下端口是否在监听：关着的端口立刻失败，别交给 adb 去等握手超时。"""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(('127.0.0.1', port))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _connect_many(ports, timeout=5):
    """并行 connect：挨个连的话，几个连不通的端口就能把界面卡住半分钟。"""
    threads = []
    for p in ports:
        t = threading.Thread(target=_adb_raw,
                             args=('connect', '127.0.0.1:%d' % p),
                             kwargs={'timeout': timeout}, daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout + 2)


def devices_info():
    if not os.path.isfile(ADB):
        return {'ok': False, 'error': '内置 adb 缺失', 'devices': []}
    try:
        _adb_raw('start-server', timeout=20)
        devs = _list_devices()
        if not any(d['state'] == 'device' for d in devs):
            # 以前这里只列 adb 已有的设备、从不主动 connect，
            # 于是模拟器明明开着也显示「未检测到」——补上主动探测。
            cands = []
            for p in _emu_ports() + KNOWN_ADB_PORTS:
                if p not in cands and _tcp_open(p):
                    cands.append(p)
            if cands:
                _connect_many(cands)
                devs = _list_devices()
                # 扫出来的 offline 条目会污染界面，顺手清掉
                for d in devs:
                    if d['state'] != 'device':
                        _adb_raw('disconnect', d['serial'], timeout=4)
                devs = _list_devices()
        devs.sort(key=lambda d: (d['state'] != 'device', d['serial']))
        # offline 基本都是扫描时连到非 adb 端口留下的僵尸条目，别让它污染界面；
        # unauthorized 保留，那是真设备、只是没点「允许调试」。
        devs = [d for d in devs if d['state'] != 'offline']
        ports = sorted({int(d['serial'].rsplit(':', 1)[-1])
                        for d in devs if d['serial'].startswith('127.0.0.1:')
                        and d['serial'].rsplit(':', 1)[-1].isdigit()})
        return {'ok': True, 'devices': devs, 'ports': ports}
    except Exception as e:
        return {'ok': False, 'error': str(e), 'devices': []}


def start_job(b):
    cur = JOB['cur']
    if cur and cur.state == 'running':
        return {'ok': False, 'error': '已有抓取任务在进行中，请等它结束或先停止。'}
    if not os.path.isfile(GRAB):
        return {'ok': False, 'error': '找不到 scripts/grab_official.py，程序目录不完整。'}

    cmd = [sys.executable, '-u', GRAB]
    port = str(b.get('port') or '').strip()
    if port:
        if not port.isdigit():
            return {'ok': False, 'error': 'adb 端口必须是数字（不知道就留空自动探测）。'}
        cmd += ['--port', port]
    pkg = str(b.get('pkg') or '').strip()
    if pkg:
        cmd += ['--pkg', pkg]
    if b.get('clear'):
        cmd += ['--clear']
    if b.get('noAlbum'):
        cmd += ['--no-album']
    if b.get('noMuseum'):
        cmd += ['--no-museum']
    tmo = b.get('timeout')
    if tmo:
        try:
            t = int(tmo)
            if 30 <= t <= 900:
                cmd += ['--timeout', str(t)]
        except Exception:
            pass

    job = Job(b, cmd)
    JOB['cur'] = job
    threading.Thread(target=_run, args=(job,), daemon=True).start()
    return {'ok': True, 'id': job.id}


def stop_job():
    cur = JOB['cur']
    if not cur or cur.state != 'running':
        return {'ok': False, 'error': '当前没有正在进行的任务。'}
    cur.state = 'stopped'
    if cur.proc:
        try:
            cur.proc.terminate()
        except Exception:
            pass
    _push(cur, '[!] 用户手动停止。请手动重开一次游戏，恢复模拟器联网。')
    return {'ok': True}


def job_view(jid, frm):
    cur = JOB['cur']
    if not cur or cur.id != jid:
        return {'ok': False, 'error': '任务不存在或已被新的任务替换。'}
    with LOCK:
        lines = cur.lines[frm:]
        nxt = len(cur.lines)
    return {
        'ok': True, 'id': cur.id, 'from': frm, 'next': nxt, 'lines': lines,
        'state': cur.state, 'step': cur.step, 'rc': cur.rc,
        'outDir': cur.out_dir, 'saveFile': cur.save_file, 'error': cur.error,
        'elapsed': int(time.time() - cur.started),
    }


def _hw_call(cmd, port=None, extra=None, timeout=300):
    """调用 scripts/huawei_channel.py 的子命令，把它的 JSON 输出原样返回。

    拆成子进程而不是 import：华为换号要 adb root + reboot，adb 全局状态很脏，
    隔离进程能让主界面服务保持可用（退出账号期间界面不卡死）。
    """
    if not os.path.isfile(HW):
        return {'ok': False, 'error': 'scripts/huawei_channel.py 缺失，程序目录不完整。'}
    cmdline = [sys.executable, '-u', HW, cmd]
    if port:
        cmdline += ['--port', str(port)]
    if extra:
        cmdline += [str(x) for x in extra]
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUNBUFFERED'] = '1'
    try:
        p = subprocess.run(cmdline, capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=timeout, cwd=ROOT, env=env,
                           creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': '操作超时（%ss），模拟器可能无响应' % timeout}
    except Exception as e:
        return {'ok': False, 'error': str(e)}
    out = (p.stdout or '').strip()
    i = out.find('{')
    if i >= 0:
        try:
            return json.loads(out[i:])
        except Exception:
            pass
    return {'ok': False, 'error': ((out + (p.stderr or '')) or '无输出')[-400:]}


def huawei_status(port=None):
    return _hw_call('status', port=port, timeout=90)


def huawei_logout(b):
    """一键退出华为账号。破坏性操作，必须显式 confirm 才动手。"""
    if not b.get('confirm'):
        return {'ok': False, 'needConfirm': True,
                'error': '退出华为账号需要二次确认（会重启模拟器并要求重新登录）'}
    if JOB['cur'] and JOB['cur'].state == 'running':
        return {'ok': False, 'error': '正在抓取中，请先停止任务再退出账号。'}
    extra = ['--no-reboot'] if b.get('noReboot') else []
    return _hw_call('logout', port=b.get('port'), extra=extra, timeout=420)


def huawei_prepare(b):
    if JOB['cur'] and JOB['cur'].state == 'running':
        return {'ok': False, 'error': '正在抓取中，请先停止任务再准备环境。'}
    extra = []
    apk = str(b.get('apk') or '').strip()
    if apk:
        extra += ['--apk', apk]
    # 游戏 APK 可选：不传时 ensure_abi 会自动从模拟器 pm path 提取已装包，
    # 玩家不必手输路径也能一键修好 x86 加固库闪退。
    game = str(b.get('gameApk') or '').strip()
    if game:
        extra += ['--game-apk', game]
    return _hw_call('prepare', port=b.get('port'), extra=extra, timeout=600)


def _net_call(cmd, port=None, extra=None, timeout=180):
    """调用 scripts/net_restore.py，返回它的 JSON 输出。"""
    if not os.path.isfile(NET_RESTORE):
        return {'ok': False, 'error': 'scripts/net_restore.py 缺失，程序目录不完整。'}
    cmdline = [sys.executable, '-u', NET_RESTORE, cmd]
    if port:
        cmdline += ['--port', str(port)]
    if extra:
        cmdline += [str(x) for x in extra]
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUNBUFFERED'] = '1'
    try:
        p = subprocess.run(cmdline, capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=timeout, cwd=ROOT, env=env,
                           creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': '操作超时（%ss），模拟器可能无响应' % timeout}
    except Exception as e:
        return {'ok': False, 'error': str(e)}
    out = (p.stdout or '').strip()
    i = out.find('{')
    if i >= 0:
        try:
            return json.loads(out[i:])
        except Exception:
            pass
    return {'ok': False, 'error': ((out + (p.stderr or '')) or '无输出')[-400:]}


def net_status(port=None):
    return _net_call('status', port=port, timeout=90)


def net_restore(b):
    """一键恢复联网：清抓包残留。可选重启模拟器兜底。"""
    if JOB['cur'] and JOB['cur'].state == 'running':
        return {'ok': False, 'error': '正在抓取中，请先停止任务再恢复联网。'}
    extra = ['--reboot'] if b.get('reboot') else []
    return _net_call('restore', port=b.get('port'), extra=extra,
                     timeout=180 if b.get('reboot') else 120)


def _oppo_call(cmd, port=None, extra=None, timeout=180):
    """调用 scripts/oppo_channel.py，返回它的 JSON 输出。"""
    if not os.path.isfile(OPPO):
        return {'ok': False, 'error': 'scripts/oppo_channel.py 缺失，程序目录不完整。'}
    cmdline = [sys.executable, '-u', OPPO, cmd]
    if port:
        cmdline += ['--port', str(port)]
    if extra:
        cmdline += [str(x) for x in extra]
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUNBUFFERED'] = '1'
    try:
        p = subprocess.run(cmdline, capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=timeout, cwd=ROOT, env=env,
                           creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': '操作超时（%ss），模拟器可能无响应' % timeout}
    except Exception as e:
        return {'ok': False, 'error': str(e)}
    out = (p.stdout or '').strip()
    i = out.find('{')
    if i >= 0:
        try:
            return json.loads(out[i:])
        except Exception:
            pass
    return {'ok': False, 'error': ((out + (p.stderr or '')) or '无输出')[-400:]}


def oppo_status(port=None):
    return _oppo_call('status', port=port, timeout=90)


def oppo_logout(b):
    """一键退出 OPPO 账号。破坏性操作，必须显式 confirm 才动手。"""
    if not b.get('confirm'):
        return {'ok': False, 'needConfirm': True,
                'error': '退出 OPPO 账号需要二次确认（会清除游戏数据并要求重新登录）'}
    if JOB['cur'] and JOB['cur'].state == 'running':
        return {'ok': False, 'error': '正在抓取中，请先停止任务再退出账号。'}
    extra = ['--reboot'] if b.get('reboot') else []
    return _oppo_call('logout', port=b.get('port'), extra=extra, timeout=180)


def open_path(p):
    """在资源管理器里打开一个目录。若传入的是文件，则打开它所在的文件夹。"""
    if not p:
        return {'ok': False, 'error': '没有可打开的路径。'}
    ap = os.path.abspath(p)
    root = os.path.abspath(ROOT) + os.sep
    if not ap.startswith(root):
        return {'ok': False, 'error': '只允许打开程序目录内的路径。'}
    if os.path.isfile(ap):
        ap = os.path.dirname(ap)
    if not os.path.isdir(ap):
        return {'ok': False, 'error': '目录不存在：%s' % ap}
    try:
        if os.name == 'nt':
            os.startfile(ap)          # noqa: 打开资源管理器
        else:
            subprocess.Popen(['xdg-open', ap])
        return {'ok': True, 'path': ap}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


class Handler(BaseHTTPRequestHandler):
    server_version = 'TravelFrogGrabber/2.8'

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode('utf-8'))

    def _body(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except Exception:
            n = 0
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p in ('/', '/index.html'):
            fp = os.path.join(WEB_DIR, 'index.html')
            if not os.path.isfile(fp):
                return self._send(500, 'web/index.html 缺失，程序目录不完整。',
                                  'text/plain; charset=utf-8')
            with open(fp, 'rb') as f:
                return self._send(200, f.read(), 'text/html; charset=utf-8')
        if p.startswith('/assets/'):
            rel = p.lstrip('/').replace('/', os.sep)
            fp = os.path.abspath(os.path.join(WEB_DIR, rel))
            web_abs = os.path.abspath(WEB_DIR) + os.sep
            if not fp.startswith(web_abs) or not os.path.isfile(fp):
                return self._send(404, 'not found', 'text/plain; charset=utf-8')
            low = fp.lower()
            ctype = ('image/jpeg' if low.endswith(('.jpg', '.jpeg'))
                     else 'image/png' if low.endswith('.png')
                     else 'application/octet-stream')
            with open(fp, 'rb') as f:
                return self._send(200, f.read(), ctype)
        if p == '/api/status':
            return self._json(status_info())
        if p == '/api/devices':
            return self._json(devices_info())
        if p == '/api/huawei/status':
            qs = parse_qs(u.query)
            port = (qs.get('port') or [''])[0].strip() or None
            return self._json(huawei_status(port))
        if p == '/api/oppo/status':
            qs = parse_qs(u.query)
            port = (qs.get('port') or [''])[0].strip() or None
            return self._json(oppo_status(port))
        if p == '/api/net/status':
            qs = parse_qs(u.query)
            port = (qs.get('port') or [''])[0].strip() or None
            return self._json(net_status(port))
        if p == '/api/job':
            qs = parse_qs(u.query)
            jid = (qs.get('id') or [''])[0]
            try:
                frm = int((qs.get('from') or ['0'])[0] or 0)
            except Exception:
                frm = 0
            return self._json(job_view(jid, frm))
        return self._send(404, 'not found', 'text/plain; charset=utf-8')

    def do_POST(self):
        p = urlparse(self.path).path
        b = self._body()
        if p == '/api/start':
            return self._json(start_job(b))
        if p == '/api/stop':
            return self._json(stop_job())
        if p == '/api/open':
            return self._json(open_path(b.get('path') or ''))
        if p == '/api/huawei/logout':
            return self._json(huawei_logout(b))
        if p == '/api/huawei/prepare':
            return self._json(huawei_prepare(b))
        if p == '/api/oppo/logout':
            return self._json(oppo_logout(b))
        if p == '/api/net/restore':
            return self._json(net_restore(b))
        if p == '/api/quit':
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)),
                             daemon=True).start()
            return self._json({'ok': True})
        return self._send(404, 'not found', 'text/plain; charset=utf-8')


def main():
    srv = None
    port = DEFAULT_PORT
    for i in range(30):
        try:
            srv = ThreadingHTTPServer(('127.0.0.1', port + i), Handler)
            port = port + i
            break
        except OSError:
            continue
    if srv is None:
        print('[X] 8727-8756 端口都被占用，无法启动界面服务。')
        return 1
    url = 'http://127.0.0.1:%d/' % port
    print('=' * 60)
    print('  旅行青蛙 · 存档抓取器已启动')
    print('  界面地址：%s' % url)
    print('  关闭本窗口即退出程序。')
    print('=' * 60)
    if not os.environ.get('TF_NO_BROWSER'):
        threading.Timer(0.9, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            srv.server_close()
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
