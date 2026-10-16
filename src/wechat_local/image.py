"""Strict local attachment lookup and independently implemented image decoding."""

from datetime import datetime, timezone, timedelta
import hashlib
from io import BytesIO
from pathlib import Path
import re
import struct


HASH = re.compile(r"^[0-9a-fA-F]{32}$")
IMAGE_NAME = re.compile(r"^([0-9a-fA-F]{32})(?:_[hHtTwW])?\.(?:dat|png|jpg|jpeg|gif|webp|bmp)$", re.I)
V2 = b"\x07\x08\x56\x32\x08\x07"
MAX_IMAGE_BYTES = 64*1024*1024
MAX_IMAGE_PIXELS = 40_000_000
PNG = b"\x89PNG\r\n\x1a\n"
SIGNATURES = [PNG, b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM", b"RIFF"]


class ImageError(RuntimeError):
    def __init__(self, code, message, assets=None):
        super().__init__(message)
        self.code = code
        self.assets = assets or []


def _reject_reparse(path):
    try:
        if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
            raise ImageError("unsafe_asset_path", "附件路径含重解析点，未读取。")
    except FileNotFoundError:
        pass


def _check_path(path, account):
    try:
        relative = path.relative_to(account)
    except ValueError:
        raise ImageError("unsafe_asset_path", "附件路径超出已授权账号目录。") from None
    current = account
    _reject_reparse(current)
    for part in relative.parts:
        current = current / part
        _reject_reparse(current)
    if not path.resolve().is_relative_to(account.resolve()):
        raise ImageError("unsafe_asset_path", "附件路径超出已授权账号目录。")


def _quality(name):
    stem = Path(name).stem.lower()
    if stem.endswith("_t"):
        return "thumbnail"
    if stem.endswith("_h"):
        return "original"
    return "unknown"


def _verified_extension(data):
    if len(data)>MAX_IMAGE_BYTES:
        raise ImageError("image_size_exceeded","图片文件超限，未解码或调用模型。")
    try:
        from PIL import Image
    except ImportError:
        raise ImageError("image_dependency_missing", "本机图片验证组件未安装。") from None
    try:
        with Image.open(BytesIO(data)) as image:
            if image.width*image.height>MAX_IMAGE_PIXELS:
                raise ImageError("image_pixels_exceeded","图片像素超限，未加载全部像素或调用模型。")
            extension = {"JPEG": "jpg", "PNG": "png", "GIF": "gif", "WEBP": "webp",
                         "BMP": "bmp"}.get(image.format)
            if not extension:
                return None
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image.load()
        return extension
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return None


def _png_end(data, start):
    offset = start + len(PNG)
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        end = offset + length + 12
        if end > len(data):
            return None
        if data[offset + 4:offset + 8] == b"IEND":
            return end
        offset = end
    return None


def _decode_v2(data,keys):
    if not keys:
        raise ImageError("image_key_required", "该图片需要独立图片AES密钥；请在图片解码页配置，未自动取钥。")
    if len(data)<15:
        raise ImageError('image_header_invalid','图片加密头不完整。')
    plain_size,xor_size=struct.unpack_from('<II',data,6)
    cipher_size=(plain_size//16+1)*16
    if not plain_size or 15+cipher_size+xor_size>len(data) or len(data)>64*1024*1024:
        raise ImageError('image_header_invalid','图片加密区长度异常，未继续。')
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
    except ImportError:
        raise ImageError('image_dependency_missing','V2图片解码组件未安装。') from None
    try:
        head=unpad(AES.new(keys['aes'],AES.MODE_ECB).decrypt(data[15:15+cipher_size]),16)
        if len(head)!=plain_size or not head.startswith((*SIGNATURES,b'wxgf')):raise ValueError()
    except (ValueError,KeyError,TypeError):
        raise ImageError('image_key_invalid','图片AES密钥未通过验证，未生成图片。') from None
    body=data[15+cipher_size:len(data)-xor_size] if xor_size else data[15+cipher_size:]
    tail=data[-xor_size:] if xor_size else b''
    xor=keys.get('xor')
    footer=b'\xff\xd9' if head.startswith(b'\xff\xd8\xff') else b'IEND\xaeB`\x82' if head.startswith(PNG) else b';' if head.startswith(b'GIF8') else None
    if xor_size and xor is None:
        if not footer:raise ImageError('image_xor_required','此图片需要有效XOR值，未猜测像素内容。')
        check=footer[-min(len(footer),xor_size):]
        values={a^b for a,b in zip(tail[-len(check):],check)}
        if len(values)!=1:raise ImageError('image_xor_required','图片尾部未能验证唯一XOR值。')
        xor=values.pop()
    xor=0 if xor is None else xor
    decoded=head+body+tail.translate(bytes.maketrans(bytes(range(256)),bytes(x^xor for x in range(256))))
    if footer and not decoded.endswith(footer):
        raise ImageError('image_decode_invalid','图片尾部校验失败，未显示错误内容。')
    extension=_verified_extension(decoded)
    if extension:return decoded,extension
    if decoded.startswith(b'wxgf'):return _decode(decoded)
    raise ImageError('image_decode_invalid','图片解码后未通过格式／像素验证，未显示错误内容。')


def _decode(data,keys=None):
    if data.startswith(V2):
        return _decode_v2(data,keys)
    extension = _verified_extension(data)
    if extension:
        return data, extension
    if data.startswith(b"wxgf"):
        found = {}
        for signature in (PNG, b"\xff\xd8\xff"):
            start = data.find(signature, 4)
            while start >= 0:
                if signature == PNG:
                    end = _png_end(data, start)
                else:
                    marker = data.find(b"\xff\xd9", start + 3)
                    end = marker + 2 if marker >= 0 else None
                if end:
                    candidate = data[start:end]
                    extension = _verified_extension(candidate)
                    if extension:
                        found[hashlib.sha256(candidate).digest()] = (candidate, extension)
                start = data.find(signature, start + 1)
        if len(found) == 1:
            return next(iter(found.values()))
        if len(found) > 1:
            raise ImageError("image_conflict", "容器含多个不同图片，未猜测目标原件。")
        raise ImageError("wxgf_conversion_required", "WXGF未发现可验证内嵌图片；需要单独的容器转码。")
    found = {}
    for signature in SIGNATURES:
        if not data:
            continue
        key = data[0] ^ signature[0]
        prefix = bytes(byte ^ key for byte in data[:len(signature)])
        if prefix != signature:
            continue
        decoded = bytes(byte ^ key for byte in data)
        extension = _verified_extension(decoded)
        if extension:
            found[hashlib.sha256(decoded).digest()] = (decoded, extension)
    if len(found) == 1:
        return next(iter(found.values()))
    if len(found) > 1:
        raise ImageError("image_conflict", "图片存在多个不同解码结果，未猜测。")
    raise ImageError("image_format_unsupported", "图片不是已验证的普通、旧XOR或内嵌WXGF格式。")


def _write_asset(outdir, stem, suffix, data, kind, quality):
    target = outdir / (stem + suffix)
    _reject_reparse(target)
    temporary = outdir / (stem + suffix + ".new")
    _reject_reparse(temporary)
    temporary.write_bytes(data)
    temporary.replace(target)
    return {"path": str(target.resolve()), "name": target.name, "kind": kind, "quality": quality}


def selected_image_asset(account_root, message, outdir,*,image_keys=None):
    try:
        return _selected_image_asset(account_root, message, outdir,image_keys=image_keys)
    except OSError:
        raise ImageError("image_io_error", "本机图片读取或导出失败；未扩大查找范围。") from None


def _selected_image_asset(account_root, message, outdir,*,image_keys=None):
    """Return verified image assets for this message, with no broad scanning.

    ImageError.assets retains an encrypted/container source where decoding needs
    an unsupported key or converter. Such an asset is never an analyzed image.
    """
    account = Path(account_root).absolute()
    _reject_reparse(account)
    if not account.is_dir():
        raise ImageError("media_not_found", "已授权账号附件目录不存在。")
    try:
        chat = message["chat_id"]
        timestamp = message["timestamp"]
        if not isinstance(chat, str) or not chat or not isinstance(timestamp, int):
            raise ValueError()
        month = datetime.fromtimestamp(timestamp, timezone(timedelta(hours=8))).strftime("%Y-%m")
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        raise ImageError("image_locator_invalid", "图片消息缺少有效会话及时间。") from None
    chat_hash = hashlib.md5(chat.encode("utf-8")).hexdigest()
    directory = account / "msg" / "attach" / chat_hash / month / "Img"
    _check_path(directory, account)
    filenames, hashes = set(), set()
    for hint in message.get("attachment_hints", []):
        kind, value = hint.get("type"), hint.get("value")
        if kind == "filename":
            if not isinstance(value, str) or not IMAGE_NAME.fullmatch(value):
                raise ImageError("unsafe_asset_path", "图片文件名不是安全的本机指纹名称。")
            filenames.add(value.lower())
        elif kind == "hash" and isinstance(value, str) and HASH.fullmatch(value):
            hashes.add(value.lower())
        elif kind == "path":
            # Never follow database/XML path strings; use the authorized layout.
            continue
    for value in hashes:
        filenames.update(value + suffix for suffix in (
            "_h.dat", ".dat", "_t.dat", "_w.dat", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"))
    candidates = []
    for filename in sorted(filenames):
        candidate = directory / filename
        _check_path(candidate, account)
        if candidate.is_file():
            candidates.append(candidate)
    if not candidates:
        raise ImageError("media_not_found", "所选会话和月份中未找到精确匹配的图片；未扩大搜索。")
    tiers = {"original": 0, "unknown": 1, "thumbnail": 2}
    best_tier = min(tiers[_quality(path.name)] for path in candidates)
    candidates = [path for path in candidates if tiers[_quality(path.name)] == best_tier]
    sources = {}
    for candidate in candidates:
        if candidate.stat().st_size>MAX_IMAGE_BYTES:
            raise ImageError("image_size_exceeded","图片文件超限，原件保留在微信中，未读取或调用模型。")
        with candidate.open("rb") as handle:
            data=handle.read(MAX_IMAGE_BYTES+1)
        if len(data)>MAX_IMAGE_BYTES:
            raise ImageError("image_size_exceeded","图片文件在读取时超限，未解码。")
        sources.setdefault(hashlib.sha256(data).digest(), (candidate, data))
    if len(sources) != 1:
        raise ImageError("image_conflict", "多个精确候选文件内容不同，未猜测附件。")
    source, data = next(iter(sources.values()))
    quality = _quality(source.name)
    outdir = Path(outdir).absolute()
    if outdir.resolve().is_relative_to(account.resolve()):
        raise ImageError("unsafe_asset_path", "导出目录不能写入微信账号目录。")
    for part in [outdir, *outdir.parents]:
        _reject_reparse(part)
    outdir.mkdir(parents=True, exist_ok=True)
    stem = "image-" + hashlib.sha256(str(message.get("id") or (chat + str(timestamp))).encode()).hexdigest()[:24]
    try:
        decoded, extension = _decode(data,image_keys)
    except ImageError as error:
        if data.startswith(V2) or error.code in {"image_key_required", "wxgf_conversion_required", "image_format_unsupported"}:
            kind = "image_encrypted" if data.startswith(V2) else "image_container"
            error.assets = [_write_asset(outdir, stem + "-source", ".dat", data, kind, quality)]
        raise
    assets = [_write_asset(outdir, stem, "." + extension, decoded, "image", quality)]
    if decoded != data:
        source_kind = "image_container" if data.startswith(b"wxgf") else "image_encrypted"
        assets.append(_write_asset(outdir, stem + "-source", ".dat", data, source_kind, quality))
    return assets
