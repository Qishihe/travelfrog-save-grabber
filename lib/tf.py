"""单机版(travelfrog v13.1)存档加解密。保存到档必须用 js_dumps 序列化，否则校验失败。

外层结构：{"format":"travelfrog-offline-save","version":2,"app","appVersion",
           "exportedAt","exportedAtText","salt","data"(base64+异或流),"checksum"}
"""
import json, base64, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jsutil import fnv1a_js, js_dumps

SECRET = 'tf-offline::kaeru-no-tabi::2026'


def keystream(salt):
    h = int(fnv1a_js(SECRET + salt), 16)
    a = (h ^ 0x9e3779b9) & 0xFFFFFFFF
    b2 = (h * 2654435761) & 0xFFFFFFFF
    while True:
        a = (a + 0x6d2b79f5) & 0xFFFFFFFF
        t = ((a ^ (a >> 15)) * (1 | a)) & 0xFFFFFFFF
        t = ((t + ((t ^ (t >> 7)) * (61 | t))) ^ t) & 0xFFFFFFFF
        b2 = (b2 ^ (b2 << 13)) & 0xFFFFFFFF
        b2 = (b2 ^ (b2 >> 17)) & 0xFFFFFFFF
        b2 = (b2 ^ (b2 << 5)) & 0xFFFFFFFF
        yield ((t ^ (t >> 14)) ^ b2) & 255


def xs(text, salt):
    b = bytearray(text.encode('utf-8'))
    k = keystream(salt)
    for i in range(len(b)):
        b[i] ^= next(k)
    return base64.b64encode(bytes(b)).decode('ascii')


def unseal(data, salt):
    b = bytearray(base64.b64decode(data))
    k = keystream(salt)
    for i in range(len(b)):
        b[i] ^= next(k)
    return b.decode('utf-8')


def load(path):
    """返回 (存档明文 dict, 外层 env dict)。"""
    obj = json.load(open(path, encoding='utf-8'))
    plain = unseal(obj['data'], obj['salt'])
    assert fnv1a_js(SECRET + '|' + plain + '|' + str(len(SECRET))) == obj['checksum'], '校验失败'
    return json.loads(plain), obj


def save(path, plain_obj, salt, base=None):
    plain = js_dumps(plain_obj)
    obj = dict(base or {})
    obj.setdefault('format', 'travelfrog-offline-save')
    obj.setdefault('version', 2)
    obj['salt'] = salt
    obj['data'] = xs(plain, salt)
    obj['checksum'] = fnv1a_js(SECRET + '|' + plain + '|' + str(len(SECRET)))
    json.dump(obj, open(path, 'w', encoding='utf-8'), ensure_ascii=False)
    return path
