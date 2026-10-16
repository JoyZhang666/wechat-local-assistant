"""Authenticated SQLCipher v4 pages; MIT provenance in third_party/wechat-decrypt."""
import hashlib
import hmac
import os
from pathlib import Path
import struct
import tempfile

from Crypto.Cipher import AES

PAGE, RESERVE, SALT_SZ = 4096, 80, 16


def decrypt_pages(raw, data, destination, *, cancelled=None):
    """Authenticate every page before publishing; keep a prior cache on failure."""
    if len(raw) != 32 or len(data) < PAGE or len(data) % PAGE:
        raise ValueError("database_format")
    salt = data[:SALT_SZ]
    key = hashlib.pbkdf2_hmac("sha512", raw, salt, 256000, 32)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, bytes(v ^ 0x3a for v in salt), 2, 32)
    for number in range(1, len(data) // PAGE + 1):
        if cancelled and cancelled():
            raise KeyboardInterrupt()
        page = data[(number - 1) * PAGE:number * PAGE]
        offset = SALT_SZ if number == 1 else 0
        expected = hmac.new(mac_key, page[offset:PAGE - 64] + struct.pack("<I", number), hashlib.sha512).digest()
        if not hmac.compare_digest(expected, page[PAGE - 64:]):
            raise ValueError("database_authentication")
    start = PAGE - RESERVE
    first = AES.new(key, AES.MODE_CBC, data[start:start + 16]).decrypt(data[SALT_SZ:start])
    if not (first[:2] == b"\x10\x00" and first[4:6] == b"\x50\x40" and first[7] == 0x20):
        raise ValueError("database_header")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=destination.name + ".", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(b"SQLite format 3\0" + first + data[start:PAGE])
            for offset in range(PAGE, len(data), PAGE):
                if cancelled and cancelled():
                    raise KeyboardInterrupt()
                page = data[offset:offset + PAGE]
                output.write(AES.new(key, AES.MODE_CBC, page[start:start + 16]).decrypt(page[:start]) + page[start:])
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
