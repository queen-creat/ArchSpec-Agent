"""把浮点向量稳定编码为 SQLite 可保存的二进制。"""

import math
import struct
from collections.abc import Sequence


def encode_vector(vector: Sequence[float]) -> bytes:
    values = [float(value) for value in vector]
    if not values:
        raise ValueError("向量不能为空")
    if not all(math.isfinite(value) for value in values):
        raise ValueError("向量只能包含有限数字")
    return struct.pack(f"<{len(values)}f", *values)


def decode_vector(data: bytes, dimension: int) -> list[float]:
    if dimension < 1 or len(data) != dimension * 4:
        raise ValueError("向量二进制长度与声明维度不一致")
    values = list(struct.unpack(f"<{dimension}f", data))
    if not all(math.isfinite(value) for value in values):
        raise ValueError("向量只能包含有限数字")
    return values
