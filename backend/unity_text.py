"""Read the game's UnityFS text bundles using only the Python standard library.

Only TextAsset and the known CHS localization layout are accepted. Blocks are
spooled to disk, so reading CHS does not retain all ten languages in memory.
"""
from __future__ import annotations

from contextlib import contextmanager
import io
import re
import struct
import tempfile


MAX_UNPACKED = 256 * 1024 * 1024


class BundleError(ValueError):
    pass


class Reader:
    def __init__(self, stream, endian=">", start=0, size=None):
        self.stream, self.endian, self.start = stream, endian, start
        self.end = None if size is None else start + size
        stream.seek(start)

    def read(self, size):
        if size < 0 or (self.end is not None and self.stream.tell() + size > self.end):
            raise BundleError("Unity 数据越界")
        data = self.stream.read(size)
        if len(data) != size:
            raise BundleError("Unity 数据不完整")
        return data

    def value(self, fmt):
        return struct.unpack(self.endian + fmt, self.read(struct.calcsize(fmt)))[0]

    def count(self, limit=1_000_000):
        value = self.value("i")
        if not 0 <= value <= limit:
            raise BundleError("Unity 数据数量异常")
        return value

    def align(self, alignment=4):
        self.read((-(self.stream.tell() - self.start)) % alignment)

    def zero(self):
        data = bytearray()
        while len(data) < 4096:
            char = self.read(1)
            if char == b"\0":
                return data.decode("utf-8")
            data.extend(char)
        raise BundleError("Unity 字符串过长")

    def string(self):
        raw = self.read(self.count(32 * 1024 * 1024))
        self.align()
        return raw.decode("utf-8-sig")


def lz4(data, expected):
    """Decode a raw LZ4 block, including overlapping back references."""
    out, cursor = bytearray(), 0

    def length(initial):
        nonlocal cursor
        result = initial
        if initial == 15:
            while True:
                if cursor >= len(data):
                    raise BundleError("LZ4 长度不完整")
                extra = data[cursor]; cursor += 1
                result += extra
                if extra != 255:
                    break
        return result

    while cursor < len(data):
        token = data[cursor]; cursor += 1
        size = length(token >> 4)
        if cursor + size > len(data) or len(out) + size > expected:
            raise BundleError("LZ4 字面量越界")
        out.extend(data[cursor:cursor + size]); cursor += size
        if cursor == len(data):
            break
        if cursor + 2 > len(data):
            raise BundleError("LZ4 引用不完整")
        offset = data[cursor] | data[cursor + 1] << 8; cursor += 2
        size = length(token & 15) + 4
        if offset == 0 or offset > len(out) or len(out) + size > expected:
            raise BundleError("LZ4 引用越界")
        # Repeating the referenced period handles overlap without a byte loop.
        begin = len(out) - offset
        period = out[begin:begin + min(offset, size)]
        out.extend((period * ((size + offset - 1) // offset))[:size])
    if len(out) != expected:
        raise BundleError("LZ4 解压大小不符")
    return bytes(out)


def unpack(data, size, flags):
    if not 0 <= size <= MAX_UNPACKED:
        raise BundleError("Unity 解压大小异常")
    mode = flags & 63
    if mode == 0:
        if len(data) != size:
            raise BundleError("Unity 块大小不符")
        return data
    if mode in (2, 3):
        return lz4(data, size)
    raise BundleError(f"暂不支持的数据包压缩方式：{mode}")


@contextmanager
def bundle_files(path):
    with open(path, "rb") as stream, tempfile.TemporaryFile() as expanded:
        reader = Reader(stream)
        if reader.zero() != "UnityFS":
            raise BundleError("不是 UnityFS 数据包")
        version = reader.value("I")
        if version not in (6, 7, 8):
            raise BundleError("不支持的 UnityFS 版本")
        reader.zero()
        engine = reader.zero()
        total = reader.value("q")
        compressed, raw_size, flags = (reader.value("I") for _ in range(3))
        if flags & ~0x3ff or flags & 0x400:
            raise BundleError("不支持的 UnityFS 标记")
        header_end = stream.tell()
        stream.seek(0, 2)
        if stream.tell() != total:
            raise BundleError("UnityFS 文件大小不符")
        stream.seek(header_end)
        if version >= 7 or tuple(map(int, re.findall(r"\d+", engine)[:3])) >= (2019, 4, 15):
            reader.align(16)
        start = stream.tell()
        if flags & 0x80:
            stream.seek(total - compressed)
        info = Reader(io.BytesIO(unpack(reader.read(compressed), raw_size, flags)))
        info.read(16)
        blocks = [(info.value("I"), info.value("I"), info.value("H")) for _ in range(info.count(10000))]
        nodes = [(info.value("q"), info.value("q"), info.value("I"), info.zero())
                 for _ in range(info.count(10000))]
        stream.seek(start if flags & 0x80 else start + compressed)
        if flags & 0x200:
            reader.align(16)
        if sum(block[0] for block in blocks) > MAX_UNPACKED:
            raise BundleError("UnityFS 数据包展开后过大")
        for raw_size, compressed, block_flags in blocks:
            expanded.write(unpack(reader.read(compressed), raw_size, block_flags))
        size = expanded.tell()
        if any(offset < 0 or count < 0 or offset + count > size for offset, count, _, _ in nodes):
            raise BundleError("UnityFS 文件目录越界")
        yield expanded, nodes


def objects(stream, start, size):
    reader = Reader(stream, start=start, size=size)
    _, file_size, version, data_offset = (reader.value("I") for _ in range(4))
    if not 17 <= version <= 22:
        raise BundleError("不支持的 Unity 数据版本")
    endian = reader.read(1); reader.read(3)
    if version >= 22:
        reader.value("I")
        file_size, data_offset = reader.value("q"), reader.value("q")
        reader.value("q")
    if file_size != size or not 0 < data_offset < size:
        raise BundleError("Unity 数据文件头无效")
    reader.endian = ">" if endian != b"\0" else "<"
    reader.zero(); reader.value("i")
    trees = reader.read(1) != b"\0"
    types = []
    for _ in range(reader.count(1000)):
        class_id = reader.value("i")
        reader.read(1); reader.value("h")
        if class_id == 114:
            reader.read(16)
        reader.read(16)
        fields = []
        if trees:
            count, strings_size = reader.count(100000), reader.count(8 * 1024 * 1024)
            nodes = []
            for _ in range(count):
                raw = reader.read(32 if version >= 19 else 24)
                nodes.append(struct.unpack(reader.endian + "HBBIIiii", raw[:24]))
            strings = reader.read(strings_size)
            common = {349: "m_Enabled", 374: "m_GameObject", 427: "m_Name", 490: "m_Script"}
            for _, level, _, _, name, _, _, _ in nodes:
                if level == 1:
                    field = common.get(name & 0x7fffffff, "?") if name & 0x80000000 else strings[name:].split(b"\0", 1)[0].decode()
                    fields.append(field)
            if version >= 21:
                reader.read(reader.count(100000) * 4)
        types.append((class_id, fields))
    result = []
    for _ in range(reader.count(100000)):
        reader.align(); reader.value("q")
        offset = reader.value("q" if version >= 22 else "I")
        count, type_id = reader.value("I"), reader.value("i")
        if not 0 <= type_id < len(types) or offset < 0 or data_offset + offset + count > size:
            raise BundleError("Unity 对象索引无效")
        class_id, fields = types[type_id]
        if class_id in (49, 114):
            result.append((start + data_offset + offset, count, class_id, fields, reader.endian))
    return result


def text_assets(path, names=None, localization=False):
    """Return selected TextAssets, or the verified CHS localization key map."""
    result = {}
    with bundle_files(path) as (stream, files):
        for offset, size, flags, name in files:
            if not flags & 4:
                continue
            for start, count, kind, fields, endian in objects(stream, offset, size):
                reader = Reader(stream, endian, start, count)
                if kind == 49 and not localization:
                    if fields and fields != ["m_Name", "m_Script"]:
                        raise BundleError("TextAsset 结构已变化")
                    name = reader.string()
                    if names is None or name in names:
                        if name in result:
                            raise BundleError("TextAsset 名称重复")
                        result[name] = reader.read(reader.count(32 * 1024 * 1024))
                elif kind == 114 and localization:
                    if fields != ["m_GameObject", "m_Enabled", "m_Script", "m_Name", "TextItemList"]:
                        raise BundleError("简中补充包结构已变化")
                    reader.read(12); reader.read(1); reader.align(); reader.read(12)
                    if reader.string().upper() != "CHS":
                        continue
                    for _ in range(reader.count(250000)):
                        key, value = reader.string(), reader.string()
                        if key in result:
                            raise BundleError("简中补充键重复")
                        result[key] = value
    if not result:
        raise BundleError("数据包中没有所需文本")
    return result
