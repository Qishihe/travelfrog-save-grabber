# -*- utf-8 -*-
"""TLS 中间人代理：只为抓游戏 WebSocket 协议。

策略：
  - 按 SNI 分流。SNI == 目标域名 -> 终止 TLS，解密后转发到真实服务器，
    并把双向明文（WebSocket 帧）按方向落盘；
  - 其他 SNI -> 纯 TCP 透传（不解密），避免破坏资源下载/其它 SDK 流量。

用法：
    python ws_proxy.py [--listen 8443] [--host ali-lxqw-login.ejoy.com]

产物：capture/<时间>_<序号>/ 下
    meta.txt     握手信息
    c2s.bin      客户端 -> 服务器 明文
    s2c.bin      服务器 -> 客户端 明文
    handshake.txt HTTP 升级请求/响应原文
"""
import argparse
import codecs
import json
import os
import socket
import ssl
import sys
import threading
import time

# --- idna 兜底（2026-11-26 修「卡在加载界面」）----------------------------
# 精简版 runtime/python 曾漏掉 unicodedata.pyd，导致 encodings.idna 断链
# (idna -> stringprep -> unicodedata)，wrap_socket(server_hostname=sni) 每个连接
# 都抛 LookupError: unknown encoding: idna，代理无法连上游 -> 游戏一直卡加载。
# 本代理的域名全是 ASCII，注册一个 ASCII 直通的 idna codec 兜底即可；
# 若运行时本来就完整，真实 idna 优先，这里注册的搜索器不会被命中。
def _ensure_idna():
    try:
        'x'.encode('idna')          # 运行时自带，什么都不用做
        return
    except (LookupError, ImportError):
        pass
    def _enc(s, errors='strict'):
        return s.encode('ascii', errors), len(s)
    def _dec(b, errors='strict'):
        return b.decode('ascii', errors), len(b)
    def _search(name):
        if name == 'idna':
            return codecs.CodecInfo(_enc, _dec, name='idna')
        return None
    codecs.register(_search)
    print('   [warn] 运行时缺 idna/unknownata，已启用 ASCII 兜底编解码器', flush=True)

_ensure_idna()

HERE = os.path.dirname(os.path.abspath(__file__))
CAP = os.path.join(HERE, 'capture')
CA_PEM = os.path.join(HERE, 'ca.pem')
SRV_PEM = os.path.join(HERE, 'srv.pem')
SRV_KEY = os.path.join(HERE, 'srv.key')

LOCK = threading.Lock()
SEQ = [0]


def next_dir(sni):
    with LOCK:
        SEQ[0] += 1
        n = SEQ[0]
    d = os.path.join(CAP, '%s_%03d_%s' % (time.strftime('%H%M%S'), n, sni))
    os.makedirs(d, exist_ok=True)
    return d


def parse_sni(data):
    """从 TLS ClientHello 里取 SNI。非 TLS 返回 None。"""
    try:
        if len(data) < 5 or data[0] != 0x16:
            return None
        p = 5 + 4  # record hdr + handshake hdr
        p += 2 + 32  # version + random
        sid_len = data[p]
        p += 1 + sid_len
        cs_len = int.from_bytes(data[p:p + 2], 'big')
        p += 2 + cs_len
        cm_len = data[p]
        p += 1 + cm_len
        ext_total = int.from_bytes(data[p:p + 2], 'big')
        p += 2
        end = p + ext_total
        while p + 4 <= end and p + 4 <= len(data):
            etype = int.from_bytes(data[p:p + 2], 'big')
            elen = int.from_bytes(data[p + 2:p + 4], 'big')
            p += 4
            if etype == 0 and p + 5 <= len(data):
                # server_name: list_len(2) type(1) name_len(2) name
                nlen = int.from_bytes(data[p + 3:p + 5], 'big')
                if p + 5 + nlen <= len(data):
                    return data[p + 5:p + 5 + nlen].decode('ascii', 'ignore')
            p += elen
    except Exception:
        return None
    return None


def read_until(sock, marker, limit=1 << 20, timeout=20):
    sock.settimeout(timeout)
    buf = b''
    while marker not in buf and len(buf) < limit:
        ch = sock.recv(4096)
        if not ch:
            break
        buf += ch
    return buf


def take_frame(buf):
    """从字节缓冲里取出一个完整 WS 帧，返回 (opcode, payload)；不完整返回 None。
    会就地消费 buf。"""
    if len(buf) < 2:
        return None
    b0, b1 = buf[0], buf[1]
    op = b0 & 0x0F
    masked = b1 & 0x80
    ln = b1 & 0x7F
    p = 2
    if ln == 126:
        if len(buf) < p + 2:
            return None
        ln = int.from_bytes(buf[p:p + 2], 'big')
        p += 2
    elif ln == 127:
        if len(buf) < p + 8:
            return None
        ln = int.from_bytes(buf[p:p + 8], 'big')
        p += 8
    mask = None
    if masked:
        if len(buf) < p + 4:
            return None
        mask = bytes(buf[p:p + 4])
        p += 4
    if len(buf) < p + ln:
        return None
    data = bytes(buf[p:p + ln])
    if mask:
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
    del buf[:p + ln]
    return op, data


def ws_frame(payload, mask=True):
    """构造一个 WS 帧。客户端 -> 服务端必须加掩码。

    官服协议全程是 text 帧（服务端下发 0x1、真实客户端请求 0x81），
    之前误用 binary(0x82) 导致注入的 album.load_by_id_list 服务端不理、
    相册图层抓不到（代理日志只有「-> 第 N 批」没有「<- 响应」）。
    """
    b = bytearray([0x81])
    n = len(payload)
    mbit = 0x80 if mask else 0
    if n < 126:
        b.append(mbit | n)
    elif n < 65536:
        b.append(mbit | 126)
        b += n.to_bytes(2, 'big')
    else:
        b.append(mbit | 127)
        b += n.to_bytes(8, 'big')
    if mask:
        k = os.urandom(4)
        b += k
        b += bytes(c ^ k[i % 4] for i, c in enumerate(payload))
    else:
        b += payload
    return bytes(b)


class Injector:
    """在服务端响应里嗅到触发指令后，主动往连接里塞请求。

    1) 相册 layers 只在 album.load_by_id_list 里下发（album.load_all 只给 id+pic_id），
       拿到 id 列表后必须自己补一发，否则照片没贴纸、显示成空白。
    2) 美好瞬间 misc.moment_load 属于登录时下发的协议，缓存登录态常常没有，
       这里在 client.hello / client.load_role 一出现就主动补一发。

    两个请求都写进各自的 jsonl；抓完由 grab_official.py 合进协议目录，
    这样即使原始 s2c.bin 没留下来也不会丢。
    """

    def __init__(self, up, wlock, outdir, batch=120, gap=1.2):
        self.up = up
        self.wlock = wlock
        self.outdir = outdir
        self.batch = batch
        self.gap = gap
        self.sent = 0
        self.got = 0
        self._busy = False
        self.moment_sent = False
        self.moment_got = False
        # 2026-11-26：服务端对「注入请求」的回包常常**不带 cmd**（实测 album 图层、
        # moment 回包都只有 {session,data}），只按 cmd 识别会整段漏掉——这就是
        # 「补抓请求明明发出去、s2c 里也有 pic_list，但照片还是空白」的根因。
        # 改为记录自己发出的 session，回包按 session 归类。
        self.album_sessions = set()
        self.moment_sessions = set()

    def _append(self, name, obj):
        try:
            with open(os.path.join(self.outdir, name), 'a', encoding='utf-8') as f:
                f.write(json.dumps(obj, ensure_ascii=False) + '\n')
        except Exception as e:
            print('   [inject] 落盘 %s 失败: %s' % (name, e), flush=True)

    def on_text(self, text):
        try:
            o = json.loads(text)
        except Exception:
            return
        cmd = o.get('cmd')
        ses = o.get('session')

        # --- 回包识别：按「自己发出的 session」或「明确的 cmd」匹配 ---
        # 注意：museum.load 等自然下发帧也带 pic_list，绝不能按内容猜，
        # 只认我们发出去的 session（9000+），这样零误判。
        if ses in self.album_sessions or cmd == 'album.load_by_id_list':
            pics = ((o.get('data') or {}).get('pic_list')
                    if isinstance(o.get('data'), dict) else None) or []
            if pics:
                self.got += len(pics)
                self._append('album_by_id.jsonl', {'cmd': 'album.load_by_id_list', 'pics': pics})
                print('   [inject] <- album.load_by_id_list  %d 张（累计 %d）session=%s'
                      % (len(pics), self.got, ses), flush=True)
                self.album_sessions.discard(ses)
            return
        if ses in self.moment_sessions or cmd in ('misc.moment_load', 'misc_moment_load'):
            data = o.get('data') or {}
            if isinstance(data, dict) and data.get('list'):
                self.moment_got = True
                self._append('moment_by_inject.jsonl',
                             {'cmd': 'misc.moment_load', 'data': data})
                print('   [inject] <- 美好瞬间 %d 个 session=%s' % (len(data['list']), ses),
                      flush=True)
                self.moment_sessions.discard(ses)
            return

        # --- 触发：嗅到 album.load_all 后补抓图层 ---
        if cmd == 'album.load_all' and not self._busy:
            ids = [x.get('id') for x in ((o.get('data') or {}).get('id_list') or [])
                   if isinstance(x, dict) and x.get('id') is not None]
            if ids:
                print('   [inject] 拿到 %d 个相册 id，开始补抓图层' % len(ids), flush=True)
                self._busy = True
                threading.Thread(target=self._fire, args=(ids,), daemon=True).start()

    def _send(self, cmd, data, session=None):
        self.sent += 1
        ses = session if session is not None else (9000 + self.sent)
        if cmd == 'album.load_by_id_list':
            self.album_sessions.add(ses)
        elif cmd in ('misc.moment_load', 'misc_moment_load'):
            self.moment_sessions.add(ses)
        msg = {'session': ses, 'timestamp': int(time.time()), 'cmd': cmd}
        if data is not None:
            msg['data'] = data
        raw = ws_frame(json.dumps(msg, ensure_ascii=False).encode('utf-8'))
        with self.wlock:
            self.up.sendall(raw)
        return self.sent

    def _fire(self, ids):
        try:
            # 补抓美好瞬间（登录态没下发时也能拿到），与相册并行；没收到响应就下轮再试
            if not self.moment_got:
                self.moment_sent = True
                self._send('misc.moment_load', None)
                print('   [inject] -> 补抓美好瞬间 misc.moment_load', flush=True)
            for i in range(0, len(ids), self.batch):
                part = ids[i:i + self.batch]
                n = self._send('album.load_by_id_list', {'id_list': part})
                print('   [inject] -> 第 %d 批 %d 个 id（session %d）' % (n, len(part), 9000 + n),
                      flush=True)
                time.sleep(self.gap)
        except Exception as e:
            print('   [inject] 失败: %s' % e, flush=True)
        finally:
            self._busy = False


def s2c_pump(up, tls, fs, stop, injector=None):
    """服务端 -> 客户端：落盘、转发，并顺手把完整消息喂给注入器。"""
    buf = bytearray()
    try:
        while not stop.is_set():
            data = up.recv(65536)
            if not data:
                break
            fs.write(data)
            fs.flush()
            tls.sendall(data)
            if injector is None:
                continue
            buf += data
            while True:
                fr = take_frame(buf)
                if fr is None:
                    break
                op, payload = fr
                if op in (0x8, 0x9, 0xA):
                    continue
                try:
                    injector.on_text(payload.decode('utf-8', 'ignore'))
                except Exception:
                    pass
    except Exception as e:
        print('   s2c 结束: %s' % e, flush=True)
    finally:
        stop.set()
        for s in (up, tls):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass


def pump(src, dst, logf, tag, stop, wlock=None):
    """单向搬运并落盘。"""
    try:
        while not stop.is_set():
            data = src.recv(65536)
            if not data:
                break
            logf.write(data)
            logf.flush()
            if wlock is None:
                dst.sendall(data)
            else:
                with wlock:
                    dst.sendall(data)
    except Exception as e:
        print('   pump %s 结束: %s' % (tag, e), flush=True)
    finally:
        stop.set()
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass


def handle_mitm(raw, sni, d, inject_album=False):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(SRV_PEM, SRV_KEY)
    # 握手阶段保留超时，避免半开连接一直占线程
    raw.settimeout(20)
    tls = ctx.wrap_socket(raw, server_side=True)
    print('   [MITM] %s TLS 建立' % sni, flush=True)

    req = read_until(tls, b'\r\n\r\n')
    # 去掉压缩扩展，强制服务器用明文帧
    req2 = b'\r\n'.join(l for l in req.split(b'\r\n')
                        if not l.lower().startswith(b'sec-websocket-extensions'))
    if not req2.endswith(b'\r\n\r\n'):
        req2 += b'\r\n\r\n'

    up_raw = socket.create_connection((sni, 443), timeout=15)
    upctx = ssl.create_default_context()
    up = upctx.wrap_socket(up_raw, server_hostname=sni)
    up.sendall(req2)
    resp = read_until(up, b'\r\n\r\n')
    tls.sendall(resp)
    # 握手完成后必须解除超时：游戏的长连接/心跳空闲几十秒很常见，
    # 留着 15 秒超时会被代理主动掐断，表现就是一直卡在「正在读取中」。
    up.settimeout(None)
    tls.settimeout(None)

    with open(os.path.join(d, 'handshake.txt'), 'wb') as f:
        f.write(b'=== CLIENT REQUEST ===\n' + req2 + b'\n=== SERVER RESPONSE ===\n' + resp)

    fc = open(os.path.join(d, 'c2s.bin'), 'wb')
    fs = open(os.path.join(d, 's2c.bin'), 'wb')
    stop = threading.Event()
    wlock = threading.Lock()
    inj = Injector(up, wlock, d) if inject_album else None
    t = threading.Thread(target=s2c_pump, args=(up, tls, fs, stop, inj), daemon=True)
    t.start()
    pump(tls, up, fc, 'c2s', stop, wlock)
    t.join(timeout=5)
    fc.close()
    fs.close()
    print('   [MITM] %s 连接关闭，c2s=%dB s2c=%dB' %
          (sni, os.path.getsize(os.path.join(d, 'c2s.bin')),
           os.path.getsize(os.path.join(d, 's2c.bin'))), flush=True)


def handle_passthrough(raw, sni):
    up = socket.create_connection((sni, 443), timeout=15)
    # 连接建立后解除超时：透传的是长连接（账号 SDK 心跳、推送），
    # 15 秒空闲超时会把它们掐断，游戏就卡在登录/读取中。
    up.settimeout(None)
    try:
        raw.settimeout(None)
    except Exception:
        pass
    stop = threading.Event()

    class Null:
        def write(self, b):
            pass

        def flush(self):
            pass

    threading.Thread(target=pump, args=(up, raw, Null(), 'p_up', stop), daemon=True).start()
    pump(raw, up, Null(), 'p_down', stop)


# 渠道账号 / 系统服务域名：一律原样透传，绝不做 TLS 解密。
#
# 为什么：OPPO 渠道服用 --host-all 时，账号 SDK（欢太 / heytap / oppomobile）
# 的连接也会被终止 TLS。实测表现是 SDK 认证请求发不出去、连上就 read timeout，
# 游戏永远卡在登录页——玩家反馈「开着抓取器进不去游戏，不开就能进」。
# 这些域名跟存档协议无关，透传不影响抓取，但能让游戏正常认证进主界面。
PASSTHROUGH_SUFFIXES = (
    # OPPO / 欢太 / 一加
    'heytap.com', 'heytapmobi.com', 'oppomobile.com', 'oppo.com', 'coloros.com',
    'allawno.com', 'allawntech.com', 'nearme.com.cn', 'gcsdk.com',
    # vivo
    'vivo.com.cn', 'vivo.com', 'vivoglobal.com',
    # 小米
    'xiaomi.com', 'mi.com', 'miui.com', 'mi-img.com',
    # 华为 / 荣耀
    'huawei.com', 'hicloud.com', 'dbankcloud.com', 'dbankcdn.com', 'honor.com',
)


def should_passthrough(sni):
    """账号/系统域名 → 纯透传，不解密。"""
    s = (sni or '').lower().rstrip('.')
    if not s:
        return False
    for suf in PASSTHROUGH_SUFFIXES:
        if s == suf or s.endswith('.' + suf):
            return True
    return False


def client_thread(raw, sni_filter, inject_album=False, host_all=False):
    try:
        raw.settimeout(6)
        try:
            peek = raw.recv(4096, socket.MSG_PEEK)
        except socket.timeout:
            # 连上却不发 ClientHello 的空闲/预连接（HTTP2 预热、健康检查）。
            # 直接关掉即可，不要打成「连接处理异常」，否则日志里一堆假故障。
            raw.close()
            return
        sni = parse_sni(peek)
        if not sni:
            raw.close()
            return
        # peek 用的 10 秒超时只用于取 SNI；之后交给长连接转发，必须解除，
        # 否则空闲连接被断开，游戏初始化会一直重试。
        raw.settimeout(None)
        # 渠道账号/系统服务域名永远透传：解密它们会让 SDK 认证卡死，
        # 游戏直接进不去（玩家表现为「开了抓取器就进不去游戏」）。
        if should_passthrough(sni):
            print('   [透传] %s（账号/系统域名，不解密）' % sni, flush=True)
            handle_passthrough(raw, sni)
            return
        # host_all：解密其余所有 SNI（b 站服等渠道服登录域名未知时的兜底）。
        # 会话按 SNI 分目录落盘，解析脚本只认协议 cmd，无关流量会被自动忽略。
        if host_all or sni in sni_filter:
            d = next_dir(sni)
            handle_mitm(raw, sni, d, inject_album)
        else:
            handle_passthrough(raw, sni)
    except socket.timeout:
        # 握手阶段对端不发数据/不回包：客户端自己会重连，不算故障，别刷日志
        try:
            raw.close()
        except Exception:
            pass
    except Exception as e:
        print('   连接处理异常: %s' % e, flush=True)
        try:
            raw.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--listen', type=int, default=8443)
    ap.add_argument('--host', action='append', default=None)
    ap.add_argument('--host-all', action='store_true',
                    help='解密所有 SNI（b 站服等登录域名未知的渠道服兜底）')
    ap.add_argument('--inject-album', action='store_true',
                    help='嗅到 album.load_all 后自动补发 album.load_by_id_list，把相册图层抓全')
    a = ap.parse_args()
    hosts = set(a.host or ['ali-lxqw-login.ejoy.com'])
    os.makedirs(CAP, exist_ok=True)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', a.listen))
    srv.listen(64)
    print('代理已启动 0.0.0.0:%d  解密域名=%s  注入相册=%s'
          % (a.listen, '全部(HOST-ALL)' if a.host_all else sorted(hosts),
             '开' if a.inject_album else '关'), flush=True)
    while True:
        raw, addr = srv.accept()
        print('连接来自 %s' % (addr,), flush=True)
        threading.Thread(target=client_thread,
                         args=(raw, hosts, a.inject_album, a.host_all), daemon=True).start()


if __name__ == '__main__':
    main()
