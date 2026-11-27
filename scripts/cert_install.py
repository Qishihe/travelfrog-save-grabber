# -*- coding: utf-8 -*-
"""把自签 CA 真正装进「游戏进程能看到」的 Android mount namespace。

根因（2026-11-27 实测，MuMu 12 / Android 12）：
    adb shell 里的 mount --bind 只影响 shell 自己的 mount namespace。
    实测 shell mnt:[4026531840]，zygote64 mnt:[4026532201]，
    zygote mnt:[4026532202]，桌面 App mnt:[4026532208] —— 全都不同。
    所以旧版虽然打印「CA 挂载=是」，游戏进程 /proc/<pid>/root/system/etc/security/cacerts
    里根本没有 ca.pem，TLS 握手直接报：
        [SSL: SSLV3_ALERT_CERTIFICATE_UNKNOWN] sslv3 alert certificate unknown

正确做法：
    1) 在 shell namespace 挂一份（兼容 Android 9- / 共享 namespace 的老模拟器）；
    2) 用 nsenter 进入 zygote 与 zygote64 的 mount namespace，各挂一份；
       游戏 force-stop 后重新启动，会从 zygote fork，继承这份挂载；
    3) 必须校验「目标命名空间里真的能读到本包 CA 的 MD5」，而不是只数
       `mount | grep -c cacerts`（那个数字连 source 路径都会算进去，会假阳性）。

兼容：
    · Android 14 的 APEX 证书目录 /apex/com.android.conscrypt/cacerts；
    · 证书目录是 symlink 时先 readlink -f，避免重复挂同一个真实目录；
    · 文件统一 chmod 644 / chown root:root / chcon system_security_cacerts_file；
    · 卸载按 mountinfo 里「root 字段包含 TMP_DIR」反查，不误删系统/其它工具的挂载。
"""

CA_DIRS = ('/system/etc/security/cacerts',
           '/apex/com.android.conscrypt/cacerts')


def _run(sh, pid, cmd):
    """在指定 zygote namespace 里执行命令；pid=None 表示当前 shell namespace。"""
    if pid:
        return sh('nsenter -t %s -m -- %s' % (pid, cmd))
    return sh(cmd)


def zygote_pids(sh):
    """zygote64 / zygote 的 PID（32/64 位应用各继承一个，必须都挂）。"""
    out = sh('pidof zygote64; pidof zygote')
    pids = []
    for tok in (out or '').replace('\n', ' ').split():
        if tok.isdigit() and tok not in pids:
            pids.append(tok)
    return pids


def ca_dirs(sh, pid=None):
    """目标 namespace 里真实存在的证书目录（去重、解析 symlink）。"""
    dirs = []
    for d in CA_DIRS:
        out = _run(sh, pid, 'test -d %s && echo YES' % d)
        if 'YES' not in (out or ''):
            continue
        real = (_run(sh, pid, 'readlink -f %s 2>/dev/null' % d) or '').strip()
        if not real.startswith('/'):
            real = d
        if real not in dirs:
            dirs.append(real)
    return dirs


def cert_count(sh, pid, d):
    out = _run(sh, pid, 'ls %s 2>/dev/null | wc -l' % d)
    out = (out or '').strip()
    return int(out) if out.isdigit() else 0


def file_md5(sh, pid, path):
    out = _run(sh, pid, 'md5sum %s 2>/dev/null' % path)
    parts = (out or '').split()
    return parts[0].lower() if parts and len(parts[0]) == 32 else ''


def mountinfo(sh, pid=None):
    return _run(sh, pid, 'cat /proc/self/mountinfo 2>/dev/null')


def _root_is_tmp(root, tmp_dir):
    """mountinfo 的 root 字段是否指向 tmp_dir。

    坑：root 是【相对源文件系统根】的路径。/data/local/tmp/cacerts 在
    mountinfo 里常写成 /local/tmp/cacerts（/data 是 ext4 挂载根），
    甚至临时目录被删后变成 /local/tmp/cacerts//deleted。
    只按完整 tmp_dir 匹配会漏掉，清理时就变成“删了目录、挂载还在”，
    把 /system/etc/security/cacerts 遮成空目录。
    """
    root = (root or '').replace('\\040', ' ')
    tmp = (tmp_dir or '').rstrip('/')
    if not root or not tmp:
        return False
    if root == tmp or root.startswith(tmp + '/'):
        return True
    base = tmp.rsplit('/', 1)[-1]
    if len(base) < 3:
        return False
    return (root.endswith('/' + base) or
            ('/' + base + '//deleted') in root or
            root.endswith('/' + base + '/deleted'))


def mounted_targets(sh, tmp_dir, pid=None):
    """该 namespace 里由 tmp_dir 绑上去的挂载点。

    bind mount 在 mountinfo 里 source 字段是底层设备，但 root 字段（第 4 列）
    指向 tmp_dir，mount point 是第 5 列。
    """
    targets = []
    for line in (mountinfo(sh, pid) or '').splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        if not _root_is_tmp(parts[3], tmp_dir):
            continue
        mp = parts[4].replace('\\040', ' ')
        if mp not in targets:
            targets.append(mp)
    return targets


def _clean_ns(sh, pid, tmp_dir):
    """只卸掉由 tmp_dir 绑上去的挂载，绝不 umount 系统原有挂载。"""
    for _ in range(4):
        targets = mounted_targets(sh, tmp_dir, pid)
        if not targets:
            return True
        for t in targets:
            _run(sh, pid, 'umount %s 2>/dev/null' % t)
    return not mounted_targets(sh, tmp_dir, pid)


def prepare(sh, tmp_dir):
    """把系统原有根证书整体复制到 tmp_dir，供 bind mount 使用（不能只剩自签 CA）。

    返回 (ok, source_dir, count, detail)。
    """
    # 先清上一次运行可能残留的挂载：否则 /system/etc/security/cacerts
    # 会被旧挂载遮住，复制出来 0 张证书。
    try:
        unmount(sh, tmp_dir)
    except Exception:
        pass
    dirs = ca_dirs(sh)
    if not dirs:
        return False, '', 0, '找不到系统证书目录（%s）' % ' / '.join(CA_DIRS)
    src = dirs[0]
    sh('rm -rf %s; mkdir -p %s' % (tmp_dir, tmp_dir))
    sh('cp -a %s/. %s/ 2>/dev/null' % (src, tmp_dir))
    n = cert_count(sh, None, tmp_dir)
    if n < 10:      # 老 toybox 的 cp 不支持 -a 时退回 -r
        sh('cp -r %s/. %s/ 2>/dev/null' % (src, tmp_dir))
        n = cert_count(sh, None, tmp_dir)
    return (n >= 10), src, n, ('已复制 %d 张系统根证书（来源 %s）' % (n, src))


def mount(sh, tmp_dir, ca_name, ca_md5=''):
    """在 shell + 每个 zygote namespace 挂载，并按 MD5 严格校验。

    返回 dict：
        ok          True 表示至少一个「应用会继承」的命名空间校验通过
        mode        'zygote' / 'shell'
        cert_count  目标目录里证书总数（应为系统原有 + 1）
        namespaces  校验通过的 zygote pid 列表
        detail      给日志的一行说明
    """
    dirs = ca_dirs(sh)
    if not dirs:
        return {'ok': False, 'mode': '', 'cert_count': 0, 'namespaces': [],
                'detail': '找不到系统证书目录'}

    pids = zygote_pids(sh)
    # 1) shell namespace：兼容 Android 9- / 共享 namespace；Android 10+ 这层
    #    对 App 不可见，但保留不碍事，且老设备靠它。
    #    只清自己上一次的挂载，绝不 umount 系统原有的证书分区/APEX 挂载。
    _clean_ns(sh, None, tmp_dir)
    for d in dirs:
        sh('mount --bind %s %s' % (tmp_dir, d))

    # 2) zygote / zygote64 namespace：新启动的 App 从这里继承。
    good_pids = []
    details = []
    for pid in pids:
        pdirs = ca_dirs(sh, pid)
        if not pdirs:
            details.append('zygote %s 无证书目录' % pid)
            continue
        _clean_ns(sh, pid, tmp_dir)
        for d in pdirs:
            sh('nsenter -t %s -m -- mount --bind %s %s' % (pid, tmp_dir, d))
        ok = True
        for d in pdirs:
            got = file_md5(sh, pid, '%s/%s' % (d, ca_name))
            n = cert_count(sh, pid, d)
            if not got or (ca_md5 and got != ca_md5.lower()):
                ok = False
                details.append('zygote %s 的 %s 读不到本包 CA' % (pid, d))
            elif n < 10:
                ok = False
                details.append('zygote %s 的 %s 只剩 %d 张证书' % (pid, d, n))
        if ok:
            good_pids.append(pid)

    # 3) shell 侧也校验一次（老 Android 只有这层生效）
    shell_ok = True
    shell_md5 = ''
    for d in dirs:
        shell_md5 = file_md5(sh, None, '%s/%s' % (d, ca_name))
        if not shell_md5 or (ca_md5 and shell_md5 != ca_md5.lower()):
            shell_ok = False

    if pids:
        ok = len(good_pids) == len(pids) and bool(good_pids)
        mode = 'zygote 命名空间' if ok else ''
        if not ok:
            details.append('shell 侧=%s；zygote 侧 %d/%d 通过'
                           % ('可见' if shell_ok else '不可见', len(good_pids), len(pids)))
    else:
        ok = shell_ok
        mode = 'shell 命名空间（无 zygote）'
        if not ok:
            details.append('shell 侧读不到本包 CA')

    return {'ok': bool(ok), 'mode': mode, 'cert_count': cert_count(sh, None, dirs[0]),
            'namespaces': good_pids, 'detail': '；'.join(details) or '校验通过'}


def unmount(sh, tmp_dir):
    """卸载 shell + 所有 zygote namespace 里由 tmp_dir 绑定的挂载。

    返回 True 表示全部清干净。先卸 zygote，再卸 shell，最后删 tmp_dir；
    顺序反了会把 mount source 删掉，留下“空壳挂载”。
    """
    ok = True
    for pid in zygote_pids(sh) + [None]:
        if not _clean_ns(sh, pid, tmp_dir):
            ok = False
    # 只有确认全部卸载后才删临时目录；否则会留下指向 deleted inode 的挂载，
    # 把系统证书目录遮成空目录（这次真机上踩到的坑）。
    if ok:
        sh('rm -rf %s 2>/dev/null' % tmp_dir)
    return ok


def status(sh, tmp_dir, ca_name):
    """诊断用：各命名空间的挂载/证书可见性，不改动任何东西。"""
    info = {'zygotes': zygote_pids(sh), 'dirs': ca_dirs(sh), 'namespaces': {}}
    for pid in info['zygotes']:
        info['namespaces'][pid] = {
            'targets': mounted_targets(sh, tmp_dir, pid),
            'ca_md5': '',
            'cert_count': 0,
        }
        pdirs = ca_dirs(sh, pid)
        if pdirs:
            info['namespaces'][pid]['ca_md5'] = file_md5(
                sh, pid, '%s/%s' % (pdirs[0], ca_name))
            info['namespaces'][pid]['cert_count'] = cert_count(sh, pid, pdirs[0])
    info['shell_targets'] = mounted_targets(sh, tmp_dir, None)
    return info
