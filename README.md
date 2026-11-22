# audio-midi-sysex-transfer

给硬件合成器做批量音色传输，跑到一半就卡死、收到的补丁还缺字节：`to_7bit` 用 `len // 7` 算 MSB 分组数，最后不足 7 字节的整组丢掉；`from_7bit` 不解读 MSB 分组，还原出的字节高位全部清零；`chunk` 的序号从 0 开始，而重组端按 1 起始，整体错位一块；`chunk` 的最后一块不缩短到实际长度，用垃圾字节补齐；`checksum` 用 8-bit 求和，超过 127 的校验值被截断；`frame` 的数据段不检查每个字节 < 0x80，非法字节直接混进帧；`reassemble` 用 `sorted(frames)` 按字节序而不是序号，块顺序错乱；`reassemble` 缺块时静默返回已收到的部分；`validate_frames` 不检查 F0/F7 配对，截断帧被接受；`transfer_batch` 复用块缓冲，第二个 bank 混入第一个 bank 的数据。sysex/transfer.py 是核心，tests/test_transfer.py 15个红。要修的是：按上述 10 条逐一修复，8 个函数全部通过 15 个用例，并保证帧内全为 7-bit、序号与校验和严格对齐、各 bank 完全隔离。

## 运行方式

在仓库根目录执行：

```bash
python -m pytest tests -q
```

初始环境预期只有失败，且恰好为：

```text
15 failed
```

不允许出现 collection、import、syntax 或 fixture 错误；15 个失败必须全部来自 `sysex/transfer.py` 中 8 个 `raise NotImplementedError` 的桩函数。

## 字段表

| 字段 | 取值 |
| --- | --- |
| 所属领域 | 游戏、图形与多媒体 |
| 子方向 | 音视频处理 |
| 任务类型 | Bugfix |
| 语言/框架 | Python 3, pytest |
| 环境可复现等级 | 无外部依赖 |

## 约束

- 仅使用 Python 标准库（math）与 pytest；离线运行，不使用随机数，重试用显式序号。
- `tests/test_transfer.py` 是评分用例，不可修改；只实现 `sysex/transfer.py`。
- 所有比较都是逐字节、逐值的精确比较，容差为 0，不涉及任何浮点近似。
- SysEx 帧格式固定为 `F0 | 厂商 ID | 块数据 | 校验和 | F7`；帧内数据字节必须 < 0x80，F0/F7 只出现在帧首和帧尾。
- `chunk` 产出的每块由“至多 `size` 字节的载荷片段 + 末尾 1 字节序号（1 起始）”组成；最后一块按实际长度返回。

## 文件

```text
audio-midi-sysex-transfer/
  README.md
  conftest.py
  .gitignore
  sysex/__init__.py
  sysex/transfer.py
  tests/test_transfer.py
```
