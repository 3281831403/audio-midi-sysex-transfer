"""硬件合成器批量音色传输的核心实现。

数据链路
--------
发送：``payload`` --to_7bit--> 7-bit 打包数据 --chunk--> 带序号的块
--frame--> ``F0 | 厂商 ID | 块数据 | 校验和 | F7`` 帧。

接收：帧 --reassemble--> 7-bit 打包数据 --from_7bit--> ``payload``。

全局约定
--------
* 帧内数据（厂商 ID、块数据、校验和）的每个字节都必须 < 0x80；``F0``/``F7``
  只允许出现在帧首和帧尾。
* 所有比较都是逐字节、逐值的精确比较，容差为 0，不做任何浮点舍入或近似。
* 只使用标准库 ``math`` 与 pytest；不使用随机数，重试序号由调用方显式给出。
"""

from __future__ import annotations

import math

SOX = 0xF0
EOX = 0xF7
SEVEN_BIT = 0x7F
GROUP_SIZE = 7


def to_7bit(data: bytes) -> list[int]:
    """把 8-bit 字节串打包成 7-bit 值序列（每 7 字节一组，MSB 分组）。"""
    data = bytes(data)
    if not data:
        return []
    packed: list[int] = []
    # 组数用 ceil，最后不足 7 字节的组也必须输出。
    for start in range(0, len(data), GROUP_SIZE):
        group = data[start : start + GROUP_SIZE]
        msb = 0
        for index, byte in enumerate(group):
            if byte & 0x80:
                msb |= 1 << index
        packed.append(msb)
        packed.extend(byte & SEVEN_BIT for byte in group)
    return packed


def from_7bit(packed: list[int]) -> bytes:
    """把 :func:`to_7bit` 的输出还原为原始 8-bit 字节串。"""
    packed = list(packed)
    for value in packed:
        if not isinstance(value, int) or not 0 <= value <= SEVEN_BIT:
            raise ValueError(f"packed value out of 7-bit range: {value!r}")
    if not packed:
        return b""
    if len(packed) % 8 == 1:
        raise ValueError("invalid packed length: trailing group has no data bytes")

    out = bytearray()
    start = 0
    while start < len(packed):
        remaining = len(packed) - start
        # 满组 8 个值（1 MSB + 7 数据）；最后一组数据字节数 = 剩余值个数 - 1。
        group_len = 8 if remaining >= 8 else remaining
        group = packed[start : start + group_len]
        msb = group[0]
        data = group[1:]
        if msb >> len(data):
            raise ValueError("MSB byte sets bits beyond the group's data bytes")
        for index, value in enumerate(data):
            byte = value & SEVEN_BIT
            if msb & (1 << index):
                byte |= 0x80
            out.append(byte)
        start += group_len
    return bytes(out)


def chunk(payload: bytes, size: int) -> list[bytes]:
    """把 payload 切成带 1 起始序号的块。"""
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        raise ValueError(f"size must be an int >= 1, got {size!r}")
    payload = bytes(payload)
    if not payload:
        return []
    count = math.ceil(len(payload) / size)
    if count > SEVEN_BIT:
        raise ValueError(f"too many chunks: {count} > 127")
    blocks = []
    for index in range(count):
        # 序号从 1 开始；最后一块按实际长度切片，不补齐。
        piece = payload[index * size : (index + 1) * size]
        blocks.append(piece + bytes([index + 1]))
    return blocks


def checksum(data: bytes) -> int:
    """计算 7-bit 校验和：所有字节相加后对 128 取模。"""
    return sum(bytes(data)) % 128


def _check_vendor(vendor: bytes) -> bytes:
    vendor = bytes(vendor)
    if len(vendor) == 1:
        if vendor[0] == 0x00:
            raise ValueError("1-byte vendor ID must not start with 0x00")
    elif len(vendor) == 3:
        if vendor[0] != 0x00:
            raise ValueError("3-byte vendor ID must start with 0x00")
    else:
        raise ValueError(f"vendor ID must be 1 or 3 bytes, got {len(vendor)}")
    if any(byte >= 0x80 for byte in vendor):
        raise ValueError("vendor ID bytes must be < 0x80")
    return vendor


def frame(chunk: bytes, vendor: bytes) -> bytes:
    """把一块数据封装成 SysEx 帧：``F0 | 厂商 ID | 块数据 | 校验和 | F7``。"""
    vendor = _check_vendor(vendor)
    chunk = bytes(chunk)
    if len(chunk) < 1:
        raise ValueError("chunk must contain at least the sequence byte")
    # 帧内每个数据字节都必须 < 0x80，非法字节直接拒绝。
    if any(byte >= 0x80 for byte in chunk):
        raise ValueError("chunk contains bytes >= 0x80")
    return bytes([SOX]) + vendor + chunk + bytes([checksum(chunk), EOX])


def _parse_frame(raw: bytes) -> bytes:
    """解析一帧，返回块数据（含序号字节）；非法结构抛 ``ValueError``。"""
    raw = bytes(raw)
    if len(raw) < 4:
        raise ValueError("frame too short")
    if raw[0] != SOX:
        raise ValueError("frame does not start with F0")
    if raw[-1] != EOX:
        raise ValueError("frame does not end with F7")
    vendor_len = 3 if raw[1] == 0x00 else 1
    body = raw[1:-1]
    if len(body) < vendor_len + 2:  # 厂商 ID + 至少序号字节 + 校验和
        raise ValueError("frame too short for vendor ID, data and checksum")
    data = body[vendor_len:]
    if any(byte >= 0x80 for byte in body):
        raise ValueError("frame contains bytes >= 0x80")
    chunk_data, check = data[:-1], data[-1]
    if checksum(chunk_data) != check:
        raise ValueError("checksum mismatch")
    return chunk_data


def reassemble(frames: list[bytes]) -> bytes:
    """按块的序号重组出原始（已 7-bit 打包的）数据。"""
    if not frames:
        return b""
    blocks = []
    for raw in frames:
        chunk_data = _parse_frame(raw)
        blocks.append((chunk_data[-1], chunk_data[:-1]))
    sequences = [seq for seq, _ in blocks]
    # 序号必须从 1 连续到 N：缺块、重号、从 0 或 2 开始都抛错。
    if sorted(sequences) != list(range(1, len(blocks) + 1)):
        raise ValueError(f"sequence numbers must be 1..{len(blocks)}, got {sequences}")
    # 按序号排序拼接，不依赖输入顺序。
    blocks.sort(key=lambda item: item[0])
    return b"".join(piece for _, piece in blocks)


def validate_frames(frames: list[bytes]) -> list[str]:
    """检查一组帧的结构，返回问题描述列表；空列表表示全部合法。"""
    problems: list[str] = []
    sequences: list[int] = []
    for index, raw in enumerate(frames):
        raw = bytes(raw)
        if len(raw) < 4:
            problems.append(f"frame {index}: too short ({len(raw)} bytes)")
            continue
        if raw[0] != SOX:
            problems.append(f"frame {index}: missing F0 start byte")
        if raw[-1] != EOX:
            problems.append(f"frame {index}: missing F7 end byte")
        if raw[0] != SOX or raw[-1] != EOX:
            continue
        body = raw[1:-1]
        vendor_len = 3 if body and body[0] == 0x00 else 1
        if len(body) < vendor_len + 2:
            problems.append(f"frame {index}: too short for vendor ID, data and checksum")
            continue
        vendor = body[:vendor_len]
        try:
            _check_vendor(vendor)
        except ValueError as exc:
            problems.append(f"frame {index}: invalid vendor ID: {exc}")
        if any(byte >= 0x80 for byte in body):
            problems.append(f"frame {index}: data byte >= 0x80")
            continue
        chunk_data, check = body[vendor_len:-1], body[-1]
        if checksum(chunk_data) != check:
            problems.append(
                f"frame {index}: checksum mismatch "
                f"(expected {checksum(chunk_data)}, got {check})"
            )
        sequences.append(chunk_data[-1])
    if sequences and sorted(sequences) != list(range(1, len(sequences) + 1)):
        problems.append(
            f"sequence numbers must run 1..{len(sequences)}, got {sorted(sequences)}"
        )
    return problems


def transfer_batch(banks: dict[str, bytes], config: dict) -> dict:
    """批量发送多个 bank：每个 bank 独立打包、分块、成帧。"""
    if not isinstance(config, dict):
        raise ValueError("config must be a dict")
    if "vendor" not in config:
        raise ValueError("config missing required key 'vendor'")
    if "chunk_size" not in config:
        raise ValueError("config missing required key 'chunk_size'")
    vendor = _check_vendor(config["vendor"])
    chunk_size = config["chunk_size"]
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size < 1:
        raise ValueError(f"chunk_size must be an int >= 1, got {chunk_size!r}")
    pack_7bit = config.get("pack_7bit", True)
    if not isinstance(pack_7bit, bool):
        raise ValueError("pack_7bit must be a bool")

    result = {}
    for name, payload in banks.items():
        payload = bytes(payload)
        if pack_7bit:
            packed = bytes(to_7bit(payload))
        else:
            if any(byte >= 0x80 for byte in payload):
                raise ValueError(
                    f"bank {name!r}: payload contains bytes >= 0x80 "
                    "but pack_7bit is False"
                )
            packed = payload
        # 每个 bank 独立分块、成帧，不共享任何缓冲。
        blocks = chunk(packed, chunk_size)
        frames = [frame(block, vendor) for block in blocks]
        result[name] = {
            "frames": frames,
            "chunk_count": len(blocks),
            "size": len(payload),
            "checksum": checksum(packed),
        }
    return result
