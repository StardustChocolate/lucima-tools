"""Small, bounded UnityFS/TextAsset reader for the official game data bundles.

Only UnityFS 6–8 and serialized files 17–22 are supported. Unknown formats fail
before publication. Blocks are expanded to disk, not one large in-memory image.
No executable Unity objects or downloaded Python are loaded.
"""
from __future__ import annotations

import io
import re
import struct
from pathlib import Path

MAX_BLOCK = 32 * 1024 * 1024
MAX_TOTAL = 768 * 1024 * 1024


class Reader:
    def __init__(self, stream, endian='>'):
        self.stream, self.endian = stream, endian

    def read(self, size):
        if not 0 <= size <= MAX_TOTAL:
            raise ValueError('游戏资料长度无效')
        value = self.stream.read(size)
        if len(value) != size:
            raise ValueError('游戏资料下载不完整')
        return value

    def number(self, fmt):
        return struct.unpack(self.endian + fmt, self.read(struct.calcsize(fmt)))[0]

    def align(self, size=4):
        self.stream.seek((-self.stream.tell()) % size, 1)

    def string(self):
        value = bytearray()
        while len(value) < 4096:
            char = self.read(1)
            if char == b'\0':
                return value.decode('utf-8')
            value.extend(char)
        raise ValueError('游戏资料字符串过长')


def lz4_block(data, expected):
    if not 0 <= expected <= MAX_BLOCK:
        raise ValueError('游戏资料压缩块过大')
    output = bytearray()
    index = 0
    while index < len(data):
        token = data[index]
        index += 1
        literal = token >> 4
        if literal == 15:
            while True:
                extra = data[index]; index += 1; literal += extra
                if extra != 255:
                    break
        if index + literal > len(data) or len(output) + literal > expected:
            raise ValueError('游戏资料压缩块无效')
        output.extend(data[index:index + literal]); index += literal
        if index == len(data):
            break
        offset = data[index] | data[index + 1] << 8; index += 2
        length = (token & 15) + 4
        if (token & 15) == 15:
            while True:
                extra = data[index]; index += 1; length += extra
                if extra != 255:
                    break
        if not offset or offset > len(output) or len(output) + length > expected:
            raise ValueError('游戏资料压缩引用无效')
        start = len(output) - offset
        pattern = output[start:start + min(offset, length)]
        output.extend((pattern * ((length + offset - 1) // offset))[:length])
    if len(output) != expected:
        raise ValueError('游戏资料解压长度不符')
    return output


def decompress(data, size, flags):
    kind = flags & 63
    if kind == 0 and len(data) == size:
        return data
    if kind in (2, 3):
        return lz4_block(data, size)
    raise ValueError('游戏资料使用了尚不支持的压缩格式')


def extract_texts(bundle: Path, output: Path, *, wanted=None):
    """Return the verified TextAsset names written under output (flat files)."""
    output.mkdir(parents=True, exist_ok=True)
    scratch = output / '.bundle-blocks'
    try:
        with bundle.open('rb') as source, scratch.open('w+b') as expanded:
            reader = Reader(source)
            if reader.string() != 'UnityFS':
                raise ValueError('游戏资料资源包签名无效')
            version = reader.number('I')
            if version not in (6, 7, 8):
                raise ValueError('游戏资料资源包版本不受支持')
            reader.string(); reader.string()
            total = reader.number('Q')
            compressed, plain, flags = (reader.number('I') for _ in range(3))
            if total != bundle.stat().st_size or compressed > MAX_BLOCK or plain > MAX_BLOCK:
                raise ValueError('游戏资料资源包长度无效')
            if version >= 7:
                reader.align(16)
            start = source.tell()
            if flags & 128:
                source.seek(total - compressed)
            info = Reader(io.BytesIO(decompress(reader.read(compressed), plain, flags)))
            source.seek(start if flags & 128 else source.tell())
            info.read(16)
            blocks = []
            count = info.number('i')
            if not 0 < count <= 100000:
                raise ValueError('游戏资料块数量无效')
            for _ in range(count):
                blocks.append((info.number('I'), info.number('I'), info.number('H')))
            expanded_size = sum(row[0] for row in blocks)
            if expanded_size > MAX_TOTAL:
                raise ValueError('游戏资料资源包过大')
            nodes = []
            count = info.number('i')
            if not 0 < count <= 100:
                raise ValueError('游戏资料文件数量无效')
            for _ in range(count):
                offset, size = info.number('Q'), info.number('Q')
                info.number('I'); info.string()
                nodes.append((offset, size))
            if flags & 512:
                reader.align(16)
            for plain_size, compressed_size, block_flags in blocks:
                if compressed_size > MAX_BLOCK:
                    raise ValueError('游戏资料压缩块过大')
                expanded.write(decompress(reader.read(compressed_size), plain_size, block_flags))
            expanded.flush()
            names = set()
            for offset, size in nodes:
                if offset + size > expanded_size:
                    raise ValueError('游戏资料文件偏移无效')
                names.update(_serialized_texts(expanded, offset, size, output, wanted))
            return names
    finally:
        scratch.unlink(missing_ok=True)


def _serialized_texts(stream, base, size, output, wanted):
    stream.seek(base)
    reader = Reader(stream)
    reader.number('I'); file_size = reader.number('I')
    version, data_offset = reader.number('I'), reader.number('I')
    if not 17 <= version <= 22:
        raise ValueError('游戏资料对象版本不受支持')
    endian = reader.number('B'); reader.read(3)
    if version >= 22:
        reader.number('I'); file_size = reader.number('Q')
        data_offset = reader.number('Q'); reader.number('Q')
    if file_size != size or endian not in (0, 1):
        raise ValueError('游戏资料对象文件无效')
    reader.endian = '<' if endian == 0 else '>'
    reader.string(); reader.number('i')
    type_tree = reader.number('B')
    types = []
    type_count = reader.number('i')
    if not 0 < type_count < 1000:
        raise ValueError('游戏资料对象类型数量无效')
    for _ in range(type_count):
        class_id = reader.number('i'); types.append(class_id)
        reader.number('B'); reader.number('h')
        if class_id == 114:
            reader.read(16)
        reader.read(16)
        if type_tree:
            nodes, strings = reader.number('i'), reader.number('i')
            if not 0 <= nodes <= 100000 or not 0 <= strings <= MAX_BLOCK:
                raise ValueError('游戏资料类型树无效')
            reader.read(nodes * (32 if version >= 19 else 24) + strings)
            if version >= 21:
                count = reader.number('i')
                if not 0 <= count < 100000:
                    raise ValueError('游戏资料类型依赖无效')
                reader.read(count * 4)
    count = reader.number('i')
    if not 0 <= count <= 100000:
        raise ValueError('游戏资料对象数量无效')
    objects = []
    for _ in range(count):
        reader.align(); reader.number('q')
        offset = reader.number('q' if version >= 22 else 'I')
        length, type_id = reader.number('I'), reader.number('i')
        if not 0 <= type_id < len(types) or offset < 0 or offset + length + data_offset > size:
            raise ValueError('游戏资料对象偏移无效')
        if types[type_id] == 49:
            objects.append((base + data_offset + offset, length))
    names = set()
    for offset, length in objects:
        stream.seek(offset)
        name_size = reader.number('i')
        if not 0 < name_size <= 200:
            raise ValueError('游戏资料文本名称无效')
        name = reader.read(name_size).decode('utf-8')
        reader.align()
        text_size = reader.number('i')
        if text_size < 0 or stream.tell() + text_size > offset + length:
            raise ValueError('游戏资料文本长度无效')
        if not re.fullmatch(r'[A-Za-z0-9_]+', name):
            raise ValueError('游戏资料文本名称不安全')
        if wanted is None or name in wanted:
            if name in names:
                raise ValueError('游戏资料包含重复文本')
            (output / ('sd_' + name + '.txt')).write_bytes(reader.read(text_size))
            names.add(name)
    return names
