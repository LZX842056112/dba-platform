"""★ DoD: P0-3 手工核对——缓存 token 不得重复计费。

    prompt_tokens=1000, cached_tokens=600  →  可计费输入 = 400

这是 B2/B3 验收里明确要求「手工核对」的一条。
"""

from __future__ import annotations

import datetime as dt

from dba.capabilities.cost import CostNormalizer, PriceCache
from dba.capabilities.cost.normalize import billable_input_tokens
from dba.capabilities.cost.price_cache import PriceRow


def _price() -> PriceRow:
    return PriceRow(
        provider="openai",
        model="gpt-4o",
        billing_unit="PER_1K_TOKEN",
        input_price_micro_usd=2500,  # $2.50 / 1M
        output_price_micro_usd=10000,  # $10.00 / 1M
        cache_read_price_micro_usd=1250,  # $1.25 / 1M
        cache_write_price_micro_usd=0,
        currency="USD",
        fx_rate_to_usd=1.0,
        effective_from=dt.datetime(2026, 1, 1),
        effective_to=None,
        price_book_id=7,
    )


def test_billable_input_tokens_p0_3() -> None:
    # 核心断言：1000 - 600 = 400
    assert billable_input_tokens(1000, 600) == 400
    # clamp：cached > prompt 也不能为负
    assert billable_input_tokens(100, 999) == 0
    assert billable_input_tokens(1000, 0) == 1000


def test_normalize_does_not_double_bill_cache() -> None:
    cache = PriceCache()
    cache.load_rows([])
    normalizer = CostNormalizer(cache)
    rec = {
        "provider": "openai",
        "model": "gpt-4o",
        "prompt_tokens": 1000,
        "cached_tokens": 600,
        "completion_tokens": 0,
    }
    cost = normalizer.normalize_with_price(rec, _price())  # type: ignore[arg-type]

    # 可计费输入 400 → 400 * 2500 / 1000 = 1000 micro_usd
    assert cost.input_micro_usd == 1000
    # 缓存 600 → 600 * 1250 / 1000 = 750 micro_usd
    assert cost.cached_micro_usd == 750
    assert cost.output_micro_usd == 0
    # 总计 1750，而**不是** v1 的错误值 (1000*2500/1000 + 600*1250/1000) = 2500+750 = 3250
    assert cost.total_micro_usd == 1750
    assert cost.total_micro_usd != 3250
    assert cost.price_book_id == 7
    assert cost.unknown_price is False


def test_normalize_unknown_price_not_silently_zero() -> None:
    cache = PriceCache()  # 空价格表
    normalizer = CostNormalizer(cache)
    cost = normalizer.normalize(
        {"provider": "openai", "model": "no-such-model", "prompt_tokens": 10}  # type: ignore[typeddict-item]
    )
    assert cost.unknown_price is True
    assert cost.price_book_id is None


def test_normalize_billable_input_not_double_billed() -> None:
    cache = PriceCache()
    normalizer = CostNormalizer(cache, default_provider="openai")
    normalizer._prices.load_rows(  # noqa: SLF001 - 测试注入
        [
            {
                "id": 3,
                "provider": "openai",
                "model": "gpt-4o",
                "billing_unit": "PER_1K_TOKEN",
                "input_price_micro_usd": 2500,
                "output_price_micro_usd": 10000,
                "cache_read_price_micro_usd": 1250,
                "currency": "USD",
                "fx_rate_to_usd": 1.0,
                "effective_from": dt.datetime(2026, 1, 1),
                "effective_to": None,
            }
        ]
    )
    rec = {
        "provider": "openai",
        "model": "gpt-4o",
        "prompt_tokens": 1000,
        "cached_tokens": 600,
        "completion_tokens": 200,
    }
    cost = normalizer.normalize(rec)  # type: ignore[arg-type]
    # 可计费输入 = 1000 - 600 = 400，缓存 600 token 按缓存读价单独计，不重复计费
    assert billable_input_tokens(1000, 600) == 400
    assert cost.input_micro_usd == 1000  # 400 * 2500 / 1000
    assert cost.cached_micro_usd == 750  # 600 * 1250 / 1000
    assert cost.output_micro_usd == 2000  # 200 * 10000 / 1000
    assert cost.total_micro_usd == 1000 + 750 + 2000
