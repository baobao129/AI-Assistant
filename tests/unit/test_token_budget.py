"""保守 token 预算的边界与性质测试（ADR-038 决策 5）。"""

from __future__ import annotations

import math

import pytest

from backend.infrastructure.token_budget import estimate_tokens, is_cjk, truncate_to_tokens


def test_empty_text_costs_nothing():
    assert estimate_tokens("") == 0


def test_cjk_characters_are_counted_one_token_each():
    assert estimate_tokens("中文测试") == 4
    assert estimate_tokens("。，、！") == 4
    assert is_cjk("中")
    assert is_cjk("，")
    assert not is_cjk("a")
    assert not is_cjk("1")


def test_non_cjk_characters_are_three_per_token():
    assert estimate_tokens("abcdef") == 2
    assert estimate_tokens("hello world") == math.ceil(11 / 3)


def test_the_estimate_is_not_the_character_count():
    # P3 的硬要求：不得把字符数（尤其中文字符数）直接等同 token 数。
    text = "Answer the question 用中文回答这个问题 with mixed scripts 12345"
    assert estimate_tokens(text) != len(text)
    assert estimate_tokens(text) < len(text)


def test_mixed_scripts_cost_more_than_pure_latin_of_the_same_length():
    assert estimate_tokens("中" * 10) > estimate_tokens("a" * 10)


def test_the_estimate_is_monotonic_and_bounded_below():
    text = "中文 mixed 内容 with 123 numbers"
    for cut in range(len(text) + 1):
        prefix = text[:cut]
        # 单调：前缀的估计不超过全文的估计。
        assert estimate_tokens(prefix) <= estimate_tokens(text)
    # 上界性质：每个字符至少贡献 1/3 个 token。
    assert estimate_tokens(text) >= math.ceil(len(text) / 3)


def test_truncate_returns_the_whole_text_when_it_fits():
    assert truncate_to_tokens("短文本", 100) == ("短文本", False)


def test_truncate_respects_the_budget():
    text = "中" * 50 + " latin words here " * 20
    piece, truncated = truncate_to_tokens(text, 10)
    assert truncated is True
    assert estimate_tokens(piece) <= 10
    assert text.startswith(piece)


def test_truncate_with_a_zero_budget_yields_nothing():
    assert truncate_to_tokens("anything", 0) == ("", True)
    assert truncate_to_tokens("", 0) == ("", False)


@pytest.mark.parametrize("budget", [1, 3, 7, 25])
def test_truncated_prefix_is_the_longest_that_fits(budget: int):
    text = "abcdefg hijklmnop qrstuv wx yz " * 6
    piece, truncated = truncate_to_tokens(text, budget)
    if truncated:
        # 再加一个字符就会超预算：说明取到的是最长可行前缀。
        assert estimate_tokens(piece + text[len(piece)]) > budget
