"""保守的 token 预算估算（ADR-038 决策 5）。

**这不是真正的 tokenizer**，而是一个**故意偏大**的上界估计：

- CJK 字符（含中日韩标点与全角符号）按 **1 token/字符** 计；
- 其余字符按 **1/3 token/字符** 计（即 3 字符 ≈ 1 token，比英文常见的 4 字符/token 更保守）；
- 求和后取上取整。

因此 `estimate_tokens(text) >= 真实 token 数` 在两种脚本下都成立，用它做预算只会
**偏小地**使用模型窗口，绝不会悄悄超出。P3 的硬要求是「不得把中文字符数直接等同
token 数」——这里中文字符确实按 1 计，但**英数字符不再按 1 计**，且与字符数是两个量
（`token_count != len(text)`），预算也按 token 而不是按字符执行。

如果将来能在离线环境拿到目标模型的真实 tokenizer，**只需要替换本模块**。
"""

from __future__ import annotations

import math

__all__ = ["estimate_tokens", "is_cjk", "truncate_to_tokens"]

# CJK 与相关全角区间。用区间判断而不是 unicodedata.name()：更快，且不依赖
# Unicode 数据库版本差异（同一文本在任何 Python 版本上给出同一预算）。
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x2E80, 0x2EFF),  # CJK 部首补充
    (0x3000, 0x303F),  # CJK 标点
    (0x3040, 0x30FF),  # 平假名 / 片假名
    (0x3100, 0x312F),  # 注音符号
    (0x3400, 0x4DBF),  # CJK 扩展 A
    (0x4E00, 0x9FFF),  # CJK 基本区
    (0xF900, 0xFAFF),  # CJK 兼容表意
    (0xFF00, 0xFFEF),  # 全角形式
    (0xAC00, 0xD7AF),  # 谚文音节
)

# 每个非 CJK 字符折算的 token 数（1/3）：偏大，安全方向。
_NON_CJK_TOKENS_PER_CHAR = 1.0 / 3.0


def is_cjk(character: str) -> bool:
    """该字符是否属于按 1 token/字符 计费的 CJK 区间。"""

    code_point = ord(character)
    return any(low <= code_point <= high for low, high in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """保守估计文本的 token 数（单调、有界、对两种脚本都是上界）。"""

    if not text:
        return 0
    cjk = 0
    other = 0
    for character in text:
        if is_cjk(character):
            cjk += 1
        else:
            other += 1
    return math.ceil(cjk + other * _NON_CJK_TOKENS_PER_CHAR)


def truncate_to_tokens(text: str, budget: int) -> tuple[str, bool]:
    """把文本截断到 token 预算内，返回 ``(前缀, 是否被截断)``。

    先按字符数做一个上界估计，再二分收敛，因此对大文本也是对数级而不是逐字符扫描。
    ``budget <= 0`` 时返回空串。
    """

    if budget <= 0:
        return "", bool(text)
    if estimate_tokens(text) <= budget:
        return text, False
    # 每字符至少贡献 _NON_CJK_TOKENS_PER_CHAR 个 token，因此真实前缀长度不超过
    # ceil(budget / 每字符最小贡献)。用它作为二分的右界。
    high = min(len(text), int(math.ceil(budget / _NON_CJK_TOKENS_PER_CHAR)))
    low = 0
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    return text[:low], True
