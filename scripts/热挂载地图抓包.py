# -*- coding: utf-8 -*-
"""给「已经开好的游戏」热挂抓包环境（不重启、不 force-stop）。

场景：抓包守候脚本先挂好了环境，但用户中途重启了模拟器，
      root/证书/iptables 全丢，代理却还在 —— 游戏流量进不来。
本脚本只补环境；代理进程由守候脚本持有，不用管。

用法：python scripts/热挂载地图抓包.py
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import grab_official as g


def main():
    print('=' * 60)
    print('  热挂载抓包环境（游戏保持运行）')
    print('=' * 60)
    g.ADB[0] = g.find_adb()
    ser = g.detect_serial(None, 'com.ali.croak.mi')
    if not ser:
        print('[X] 找不到米服设备')
        return 1
    g.SER[0] = ser
    print('   设备：%s' % ser)

    # root
    if 'uid=0' not in g.sh('id'):
        g.run([g.ADB[0], '-s', ser, 'root'], timeout=30)
        time.sleep(3)
    if 'uid=0' not in g.sh('id'):
        print('[X] root 失败')
        return 1
    print('   root：OK')

    # 清残留
    try:
        if g.diag:
            g.diag.cleanup_all(g.sh, g.HOST_PROXY, g.CACERT_DIR, g.TMP_DIR)
    except Exception as e:
        print('   [!] 清理残留：%s' % e)

    # 挂证书 + DNAT（游戏已在跑，证书挂到 zygote + 应用命名空间）
    ips = g.resolve_ips()
    if not g.mitm_on(ips):
        print('[X] 挂载失败')
        return 1

    # 校验游戏进程能看到 CA
    try:
        vis = g._game_ca_visible('com.ali.croak.mi')
        print('   游戏可见 CA：%s' % ('是' if vis else '否（可能要重开游戏才能生效）'))
    except Exception:
        pass

    print('\n完成。现在请你在游戏里：')
    print('  1) 如果游戏卡在加载/证书错误，先彻底退出游戏再重开一次')
    print('  2) 进地图页，点赞、翻评论')
    print('  3) 打开邮件看漂流瓶')
    print('守候脚本会边抓边报告。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
