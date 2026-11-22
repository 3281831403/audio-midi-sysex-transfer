"""audio-midi-sysex-transfer 的评分用例。

15 个用例全部对应 ``sysex/transfer.py`` 的 8 个公开桩函数；初始仓库必须恰好
报告 ``15 failed``，且失败全部来自 ``NotImplementedError``，不允许 collection、
import、syntax 或 fixture 错误。

注意：本文件是评分基准，不可修改。
"""

import pytest

from sysex.transfer import (
    checksum,
    chunk,
    frame,
    from_7bit,
    reassemble,
    to_7bit,
    transfer_batch,
    validate_frames,
)

VENDOR = b"\x7d"


# ---------------------------------------------------------------------------
# 10 条对应缺陷的用例
# ---------------------------------------------------------------------------


def test_to_7bit_keeps_trailing_partial_group():
    """缺陷 1：用 len // 7 算分组数会丢掉最后不足 7 字节的组。"""
    assert to_7bit(b"") == []
    assert to_7bit(b"\x81\x02\x03") == [0x01, 0x01, 0x02, 0x03]

    data = bytes([0xFF, 0x00, 0x7F, 0x80, 0x01, 0x02, 0x03, 0x04])
    packed = to_7bit(data)
    # 7 字节满组 -> 8 个值；末尾 1 字节的组 -> 2 个值。
    assert len(packed) == 10
    assert packed[0] == 0b0001001  # 0xFF 的 bit7 落在 bit0，0x80 的 bit7 落在 bit3
    assert packed[8:] == [0x00, 0x04]
    assert all(0 <= value <= 0x7F for value in packed)


def test_from_7bit_restores_high_bits():
    """缺陷 2：不解读 MSB 分组会让还原出的字节丢掉最高位。"""
    assert from_7bit([]) == b""
    assert from_7bit([0x01, 0x01, 0x02, 0x03]) == b"\x81\x02\x03"

    data = bytes([0x80, 0xFF, 0x7F, 0x00, 0xC0, 0x40, 0x01])
    assert from_7bit(to_7bit(data)) == data

    with pytest.raises(ValueError):
        from_7bit([0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08])


def test_chunk_sequence_numbers_start_at_one():
    """缺陷 3：序号从 0 开始、重组按 1 起始会整体错位一块。"""
    blocks = chunk(b"abcdefghij", 4)
    assert blocks == [b"abcd\x01", b"efgh\x02", b"ij\x03"]
    assert [block[-1] for block in blocks] == [1, 2, 3]

    many = chunk(bytes(range(30)), 7)
    assert [block[-1] for block in many] == [1, 2, 3, 4, 5]


def test_chunk_last_block_is_not_padded():
    """缺陷 4：最后一块不得用垃圾字节补齐到 size。"""
    payload = b"0123456789"
    blocks = chunk(payload, 8)
    assert len(blocks) == 2
    assert blocks[-1] == b"89\x02"
    assert len(blocks[-1]) == 3
    assert b"".join(block[:-1] for block in blocks) == payload

    assert chunk(payload, 10) == [payload + b"\x01"]
    assert chunk(b"", 10) == []
    with pytest.raises(ValueError):
        chunk(payload, 0)


def test_checksum_stays_seven_bit():
    """缺陷 5：8-bit 求和会返回 >127 的值，必须折叠到 7-bit。"""
    assert checksum(b"") == 0
    assert checksum(b"\x00") == 0
    assert checksum(b"\x7f") == 0x7F
    assert checksum(b"\x64\x64") == 72  # 200 % 128，不是 200
    assert checksum(b"\x7f\x7f\x7f") == 125  # 381 % 128
    for data in (b"\xff\xff", bytes(range(200))):
        value = checksum(data)
        assert 0 <= value <= 0x7F
        assert value == sum(data) % 128


def test_frame_rejects_non_seven_bit_data():
    """缺陷 6：帧内混入 >= 0x80 的字节必须被拒绝，不能直接写进帧。"""
    with pytest.raises(ValueError):
        frame(b"\x01\x80", VENDOR)

    built = frame(b"ab\x01", VENDOR)
    assert built[0] == 0xF0
    assert built[-1] == 0xF7
    assert built[1:2] == VENDOR
    assert built[2:-2] == b"ab\x01"
    assert built[-2] == checksum(b"ab\x01")

    wide = frame(b"x\x01", b"\x00\x20\x29")
    assert wide[1:4] == b"\x00\x20\x29"

    with pytest.raises(ValueError):
        frame(b"", VENDOR)
    with pytest.raises(ValueError):
        frame(b"a\x01", b"\x00\x7d")  # 首字节 0x00 却不是 3 字节厂商 ID
    with pytest.raises(ValueError):
        frame(b"a\x01", b"\x7d\x7d")  # 首字节非 0x00 却是 2 字节


def test_reassemble_orders_by_sequence():
    """缺陷 7：sorted(frames) 按字节序排序会打乱块顺序。"""
    payload = bytes([9]) * 4 + bytes([1]) * 4 + bytes([5]) * 4
    blocks = chunk(payload, 4)
    frames = [frame(block, VENDOR) for block in blocks]

    # 帧的字节序是 F0 7D 01... < F0 7D 05... < F0 7D 09...，与序号顺序不同。
    assert reassemble([frames[2], frames[0], frames[1]]) == payload
    assert reassemble([frames[1], frames[2], frames[0]]) == payload
    assert reassemble(frames) == payload


def test_reassemble_raises_on_missing_block():
    """缺陷 8：缺块时不得静默返回已收到的部分数据。"""
    payload = b"abcdefghijkl"
    frames = [frame(block, VENDOR) for block in chunk(payload, 4)]
    assert len(frames) == 3

    with pytest.raises(ValueError):
        reassemble([frames[0], frames[2]])  # 缺第 2 块
    with pytest.raises(ValueError):
        reassemble([frames[0], frames[0], frames[1], frames[2]])  # 重号


def test_validate_rejects_unpaired_f0_f7():
    """缺陷 9：不检查 F0/F7 配对时，截断帧会被当成合法帧。"""
    frames = [frame(block, VENDOR) for block in chunk(b"abcdefgh", 4)]
    assert validate_frames(frames) == []

    missing_eox = validate_frames([frames[0][:-1]])
    assert missing_eox
    assert any("f7" in problem.lower() for problem in missing_eox)

    missing_sox = validate_frames([frames[0][1:]])
    assert missing_sox
    assert any("f0" in problem.lower() for problem in missing_sox)


def test_transfer_batch_keeps_banks_independent():
    """缺陷 10：复用块缓冲会让后一个 bank 混入前一个 bank 的数据。"""
    config = {"vendor": VENDOR, "chunk_size": 4}
    banks = {
        "lead": bytes(range(10)),
        "pad": bytes([200, 150, 100, 50, 25, 12, 6, 3, 1, 0]),
    }
    result = transfer_batch(banks, config)

    for name, payload in banks.items():
        entry = result[name]
        assert entry["size"] == len(payload)
        assert entry["chunk_count"] == len(entry["frames"])
        assert validate_frames(entry["frames"]) == []
        assert from_7bit(reassemble(entry["frames"])) == payload


# ---------------------------------------------------------------------------
# 另外 5 条：往返一致、单块帧化、校验和篡改、缺块异常、双 bank 独立
# ---------------------------------------------------------------------------


def test_roundtrip_arbitrary_200_bytes():
    """往返一致：任意 200 字节打包后必须逐字节还原。"""
    data = bytes((index * 37 + 11) % 256 for index in range(200))
    packed = to_7bit(data)
    assert len(packed) == 229  # ceil(200 / 7) == 29 组
    assert all(0 <= value <= 0x7F for value in packed)

    restored = from_7bit(packed)
    assert len(restored) == 200
    assert restored == data
    assert all(got == want for got, want in zip(restored, data))


def test_single_block_payload_frame_roundtrip():
    """单块 payload 帧化后重组，结果与原始 payload 相同。"""
    payload = bytes([0x00, 0x7F, 0x80, 0xFF, 0x2A])
    packed = bytes(to_7bit(payload))
    blocks = chunk(packed, 64)
    assert len(blocks) == 1

    built = frame(blocks[0], VENDOR)
    assert validate_frames([built]) == []
    assert reassemble([built]) == packed
    assert from_7bit(reassemble([built])) == payload


def test_validate_reports_corrupted_checksum():
    """校验和被篡改时 validate 必须报告出来。"""
    frames = [frame(block, VENDOR) for block in chunk(b"abcdefgh", 4)]
    corrupted = bytearray(frames[0])
    corrupted[-2] = (corrupted[-2] + 1) & 0x7F

    problems = validate_frames([bytes(corrupted)] + frames[1:])
    assert problems
    assert any("checksum" in problem.lower() for problem in problems)


def test_reassemble_raises_when_first_block_missing():
    """缺块（首块丢失，序号从 2 开始）时 reassemble 必须抛异常。"""
    payload = b"abcdefgh"
    frames = [frame(block, VENDOR) for block in chunk(payload, 2)]
    assert len(frames) == 4
    assert reassemble(frames) == payload

    with pytest.raises(ValueError):
        reassemble(frames[1:])


def test_two_banks_match_individual_transfers():
    """两个 bank 批量传输的结果必须与各自单独传输逐字节相同。"""
    config = {"vendor": VENDOR, "chunk_size": 5}
    bank_a = bytes((index * 3) % 256 for index in range(23))
    bank_b = bytes((255 - index * 5) % 256 for index in range(17))

    together = transfer_batch({"A": bank_a, "B": bank_b}, config)
    alone_a = transfer_batch({"A": bank_a}, config)
    alone_b = transfer_batch({"B": bank_b}, config)

    assert together["A"] == alone_a["A"]
    assert together["B"] == alone_b["B"]
    assert together["A"]["size"] == 23
    assert together["B"]["size"] == 17
