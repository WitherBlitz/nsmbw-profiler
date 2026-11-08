"""The GNU build ID of the APK's libmain.so, so a profile can be symbolized against the exact unstripped
library later. Works on a local APK, or on the installed one through adb without pulling all of it."""
from __future__ import annotations

import struct
from pathlib import Path

LIB = "lib/arm64-v8a/libmain.so"


class LocalFile:
    def __init__(self, path: Path):
        self.path = path

    def size(self) -> int:
        return self.path.stat().st_size

    def read(self, offset: int, length: int) -> bytes:
        with open(self.path, "rb") as file:
            file.seek(offset)
            return file.read(length)


class RemoteFile:
    """A file on the phone, read in 4 KiB blocks with dd."""

    def __init__(self, phone, path: str):
        self.phone = phone
        self.path = path

    def size(self) -> int:
        return int(self.phone.sh(f"stat -c %s '{self.path}'", 20).strip())

    def read(self, offset: int, length: int) -> bytes:
        block = 4096
        first, last = offset // block, (offset + length + block - 1) // block
        data = self.phone.sh_bytes(f"dd if='{self.path}' bs={block} skip={first} count={last - first} 2>/dev/null", 120)
        start = offset - first * block
        return data[start:start + length]


def _entry_data_offset(source, name: str) -> tuple[int, int, int]:
    """(offset of the stored bytes, size, compression method) of a zip entry."""
    size = source.size()
    tail_len = min(size, 65557)
    tail = source.read(size - tail_len, tail_len)
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise ValueError("not a zip file")
    cd_size, cd_offset = struct.unpack_from("<II", tail, eocd + 12)
    directory = source.read(cd_offset, cd_size)
    pos = 0
    wanted = name.encode()
    while pos + 46 <= len(directory) and directory[pos:pos + 4] == b"PK\x01\x02":
        method, = struct.unpack_from("<H", directory, pos + 10)
        comp_size, = struct.unpack_from("<I", directory, pos + 20)
        name_len, extra_len, comment_len = struct.unpack_from("<HHH", directory, pos + 28)
        local, = struct.unpack_from("<I", directory, pos + 42)
        if directory[pos + 46:pos + 46 + name_len] == wanted:
            header = source.read(local, 30)
            local_name, local_extra = struct.unpack_from("<HH", header, 26)
            return local + 30 + local_name + local_extra, comp_size, method
        pos += 46 + name_len + extra_len + comment_len
    raise KeyError(name)


def build_id(source) -> str:
    """Hex GNU build ID of libmain.so, or raises."""
    base, _, method = _entry_data_offset(source, LIB)
    if method != 0:
        raise ValueError("libmain.so is compressed in this APK")
    header = source.read(base, 64)
    if header[:4] != b"\x7fELF" or header[4] != 2:
        raise ValueError("libmain.so is not a 64-bit ELF")
    phoff, = struct.unpack_from("<Q", header, 0x20)
    phentsize, phnum = struct.unpack_from("<HH", header, 0x36)
    table = source.read(base + phoff, phentsize * phnum)
    for i in range(phnum):
        p_type, = struct.unpack_from("<I", table, i * phentsize)
        if p_type != 4:  # PT_NOTE
            continue
        p_offset, = struct.unpack_from("<Q", table, i * phentsize + 8)
        p_filesz, = struct.unpack_from("<Q", table, i * phentsize + 32)
        notes = source.read(base + p_offset, p_filesz)
        pos = 0
        while pos + 12 <= len(notes):
            namesz, descsz, note_type = struct.unpack_from("<III", notes, pos)
            name_start = pos + 12
            desc_start = name_start + ((namesz + 3) & ~3)
            if note_type == 3 and notes[name_start:name_start + namesz].rstrip(b"\0") == b"GNU":
                return notes[desc_start:desc_start + descsz].hex()
            pos = desc_start + ((descsz + 3) & ~3)
    raise ValueError("libmain.so has no GNU build ID")
