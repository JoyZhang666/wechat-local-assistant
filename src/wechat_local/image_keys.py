"""Optional image credentials, protected by the current Windows user's DPAPI."""
import json
from pathlib import Path
import re
from .common import NamedMutex,atomic_json,read_json
from .credentials import protect_key,reveal_key
from .export import _plain


def image_key(value,xor=None):
    if not isinstance(value,str):raise ValueError('图片AES密钥格式无效。')
    if re.fullmatch(r'[0-9a-fA-F]{32}',value):key=bytes.fromhex(value)
    else:
        try:key=value.encode('ascii')
        except UnicodeEncodeError:raise ValueError('图片AES密钥须为16个ASCII字符或32位十六进制。') from None
    if len(key)!=16:raise ValueError('图片AES密钥须为16个ASCII字符或32位十六进制。')
    if xor is not None and (isinstance(xor,bool) or not isinstance(xor,int) or not 0<=xor<=255):
        raise ValueError('图片XOR值须为0至255，未知可留空。')
    return {'aes':key,'xor':xor}


class ImageKeyStore:
    def __init__(self,data_dir):
        self.path=Path(data_dir)/'image-keys'/'v2.json'
        _plain(self.path)

    def save(self,value,xor=None,*,replace=True):
        parsed=image_key(value,xor)
        secret=json.dumps({'aes':parsed['aes'].hex(),'xor':xor})
        with NamedMutex(r'Local\WechatLocalImageKeySave'):
            _plain(self.path)
            if not replace and self.path.exists():raise ValueError('已有图片授权，未覆盖。')
            atomic_json(self.path,{'version':1,'dpapi':protect_key(secret)})

    def load(self):
        _plain(self.path)
        if not self.path.is_file():return None
        if self.path.stat().st_size>16384:raise ValueError('图片密钥记录异常，未读取。')
        secret=json.loads(reveal_key(read_json(self.path)['dpapi']))
        return image_key(secret['aes'],secret.get('xor'))
