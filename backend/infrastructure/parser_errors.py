"""Parser 边界的共享错误类型（ADR-036 / ADR-037）。

单独成模块是为了让 ``pdf_slicing`` 与 ``parsers`` 都能引用同一个 ``ParserError``，
而不产生模块级循环导入；``parsers`` 仍重新导出该名字，既有导入路径不变。
"""

from __future__ import annotations


class ParserError(RuntimeError):
    """文档无法被规范化为文本。

    参数是**安全错误码**（如 ``pdf_encrypted`` / ``shard_too_large``），
    摄取层据此映射为用户可见的安全消息，绝不携带 provider 原文或路径。
    """


__all__ = ["ParserError"]
