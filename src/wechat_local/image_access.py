"""Bounded offline V2 image access for explicitly selected, local JPEG samples."""
import hashlib
import re
import struct
import time

from .builtin_provider import account_identity,plain
from .image import V2,MAX_IMAGE_BYTES,SIGNATURES,ImageError,_decode
from .image_keys import ImageKeyStore


def acquire_images(data_dir,account_root,files,*,consent=False,stop=None,progress=None,seconds=90):
    if consent is not True:raise ValueError("请先确认本次本人图片离线接入授权。")
    stop=stop or (lambda:False)
    progress=progress or (lambda value:None)
    if not 1<=seconds<=120:raise ValueError("图片接入等待须为1—120秒。")
    root=plain(account_root)
    identity=account_identity(root)
    suffix=re.fullmatch(re.escape(identity)+r"_([a-fA-F0-9]{4})",root.name)
    if not root.is_dir() or not suffix:raise ValueError("该账号目录格式不支持离线图片接入。")
    if not 2<=len(files)<=9:raise ValueError("请只选择本人账号的2—9张不同V2加密JPEG图片。")
    deadline=time.monotonic()+seconds
    blobs=[]
    hashes=set()
    total=0
    for value in files:
        if stop():raise KeyboardInterrupt()
        path=plain(value)
        if path.stat().st_size>MAX_IMAGE_BYTES:raise ValueError("样本图片过大，未读取。")
        with path.open("rb") as handle:blob=handle.read(MAX_IMAGE_BYTES+1)
        total+=len(blob)
        if len(blob)>MAX_IMAGE_BYTES or total>128*1024*1024:raise ValueError("样本图片总量超限。")
        if len(blob)<15 or not blob.startswith(V2):raise ValueError("请选择V2加密图片，原文件保持。")
        size,xor_size=struct.unpack_from("<II",blob,6)
        if size!=1024 or xor_size<2 or 15+1040+xor_size>len(blob):
            raise ValueError("此加密图片布局不支持当前离线接入。")
        digest=hashlib.sha256(blob).digest()
        if digest in hashes:raise ValueError("请提供不同内容的图片，不能重复同一样本。")
        hashes.add(digest)
        blobs.append(blob)
    store=ImageKeyStore(data_dir)
    def valid(keys):
        for blob in blobs:
            if stop():raise KeyboardInterrupt()
            _decode(blob,keys)
    # Never replace a different pre-existing key under a generic consent.
    if store.path.exists():
        try:valid(store.load())
        except (ImageError,ValueError,KeyError):
            raise ValueError("已有图片授权不能验证所选样本，原授权保持；请先核对账号。") from None
        return {"images_verified":len(blobs),"reused":True}
    xor_values=set()
    for blob in blobs:
        xor=blob[-1]^0xd9
        if xor!=blob[-2]^0xff:raise ValueError("样本未通过JPEG尾部一致性检查。")
        xor_values.add(xor)
    if len(xor_values)!=1:raise ValueError("样本不属于同一可验证的图片密钥。")
    xor=xor_values.pop()
    from Crypto.Cipher import AES
    suffix=suffix[1].lower()
    for upper in range(1<<24):
        if upper%1024==0:
            if stop():raise KeyboardInterrupt()
            if time.monotonic()>=deadline:raise ValueError("离线接入超时，未保存；可重新授权尝试。")
            progress(upper/(1<<24))
        seed=str((upper<<8)|xor).encode("ascii")
        if hashlib.md5(seed).hexdigest()[:4]!=suffix:continue
        key=hashlib.md5(seed+identity.encode("utf-8")).hexdigest()[:16].encode("ascii")
        if not AES.new(key,AES.MODE_ECB).decrypt(blobs[0][15:31]).startswith(tuple(SIGNATURES)):continue
        try:valid({"aes":key,"xor":xor})
        except ImageError:continue
        if stop():raise KeyboardInterrupt()
        if time.monotonic()>=deadline:raise ValueError("离线接入超时，未保存。")
        store.save(key.hex(),xor,replace=False)
        progress(1.0)
        return {"images_verified":len(blobs),"reused":False}
    raise ValueError("所选样本未验证成功，未保存；没有改用进程扫描。")
