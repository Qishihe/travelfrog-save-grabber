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
    """在目录里挑最新的一份「官服抓取」存档（排除原始数据明文，且必须是本次生成的）。"""
    best = None
    try:
        for f in os.listdir(dirpath):
            if f.endswith('.json') and '官服抓取' in f and '原始数据' not in f:
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
        'ca': os.path.isfile(CA_PEM),
        'output': OUTPUT,
        'runtime': sys.executable,
        'running': bool(cur and cur.state == 'running'),
        'version': '1.0',
    }


def devices_info():
    if not os.path.isfile(ADB):
        return {'ok': False, 'error': '内置 adb 缺失', 'devices': []}
    try:
        p = subprocess.run([ADB, 'devices'], capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=15,
                           creationflags=CREATE_NO_WINDOW)
        devs = []
        for ln in (p.stdout or '').splitlines():
            ln = ln.strip()
            if not ln or ln.lower().startswith('list of devices'):
                continue
            parts = ln.split()
            if len(parts) >= 2:
                devs.append({'serial': parts[0], 'state': parts[1]})
        return {'ok': True, 'devices': devs}
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
    server_version = 'TravelFrogGrabber/1.0'

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
