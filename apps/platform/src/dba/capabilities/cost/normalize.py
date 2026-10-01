"""成本归一化（★ P0-3 的核心实现）。

对齐《设计文档 v2》§6.1、§5.2.3 与《实现要点清单》U6 / U8 / P0-3。

★ P0-3：缓存 token **不能重复计费**（照抄 v1 会怎样错）
-----------------------------------------------------
OpenAI/Anthropic 的 usage 里，``prompt_tokens`` 是**总输入**，其中 ``cached_tokens``
是命中缓存的那一部分。v1 直接把 ``prompt_tokens × 输入单价`` 全量计费，**又把**
``cached_tokens × 缓存价`` 再加一遍 → 缓存命中的 token 被**计费两次**，
成本系统性偏高（且缓存命中率越高、误差越大）。

v2 修法：**可计费输入 = prompt_tokens − cached_tokens**（下限 0），
再分别计：``可计费输入 × 输入价`` + ``cached_tokens × 缓存读价`` + ``completion × 输出价``。

手工核对（DoD ★）：``prompt_tokens=1000, cached_tokens=600`` → 可计费输入 = **400**。

★ U6：``normalize`` 是**同步**方法（在 ``finally``/取消路径被调用，绝不能 await）。
★ U8：价格未知 → ``NormalizedCost.unknown()``（``price_book_id=None`` + ``unknown_price=True``），
  绝不静默按 0 计（否则成本偏低、预算守卫失效）。
"""

from __future__ import annotations

from dba_runtime.telemetry import LLMCallRecord, NormalizedCost

from .price_cache import PriceCache, PriceRow

__all__ = ["CostNormalizer", "billable_input_tokens"]


def billable_input_tokens(prompt_tokens: int, cached_tokens: int) -> int:
    """★ P0-3：可计费输入 token = ``prompt_tokens − cached_tokens``（下限 0）。

    这是缓存不重复计费的唯一正确口径；两个入参都可能为负/异常，统一 clamp。
    """
    return max(0, int(prompt_tokens) - max(0, int(cached_tokens)))


def _round_div(amount_tokens: int, price_micro_usd: int, unit_size: int) -> int:
    """``tokens × 单价 / 单位`` 的**整数**四舍五入（half-up），避免浮点误差。"""
    if unit_size <= 0 or amount_tokens <= 0 or price_micro_usd <= 0:
        return 0
    return (amount_tokens * price_micro_usd + unit_size // 2) // unit_size


class CostNormalizer:
    """基于**预热价格缓存**的成本归一化器（同步，可安全用于装饰器 ``finally``）。"""

    def __init__(self, price_cache: PriceCache, *, default_provider: str = "openai") -> None:
        self._prices = price_cache
        self._default_provider = default_provider

    def normalize(self, rec: LLMCallRecord) -> NormalizedCost:
        """把一条 LLM 记录归一化成本（micro_usd）。

        未知价格 → ``NormalizedCost.unknown()``（不按 0 计）。
        """
        provider = str(rec.get("provider") or self._default_provider)
        model = str(rec.get("model") or "")
        price = self._prices.get(provider, model)
        if price is None and model:
            # 允许调用方把 provider 直接写进 model（部分网关如此），再兜一次
            price = self._prices.get(self._default_provider, model)
        if price is None:
            return NormalizedCost.unknown(rec)
        return self.normalize_with_price(rec, price)

    def normalize_with_price(self, rec: LLMCallRecord, price: PriceRow) -> NormalizedCost:
        """用既有价格行做归一化（便于单测直接给价）。"""
        unit = price.unit_size
        prompt = int(rec.get("prompt_tokens", 0) or 0)
        cached = int(rec.get("cached_tokens", 0) or 0)
        completion = int(rec.get("completion_tokens", 0) or 0)

        if price.billing_unit == "PER_CALL":
            return NormalizedCost(
                input_micro_usd=price.input_price_micro_usd,
                cached_micro_usd=0,
                output_micro_usd=0,
                price_book_id=price.price_book_id,
                unit_size=1,
                currency=price.currency,
                fx_rate_to_usd=price.fx_rate_to_usd,
                unknown_price=False,
            )
        if price.billing_unit == "PER_SECOND":
            latency_ms = int(rec.get("latency_ms", 0) or 0)
            seconds = max(1, (latency_ms + 999) // 1000) if latency_ms else 1
            return NormalizedCost(
                input_micro_usd=seconds * price.input_price_micro_usd,
                cached_micro_usd=0,
                output_micro_usd=0,
                price_book_id=price.price_book_id,
                unit_size=1,
                currency=price.currency,
                fx_rate_to_usd=price.fx_rate_to_usd,
                unknown_price=False,
            )

        # ★ P0-3：可计费输入 = 总输入 − 缓存命中
        billed = billable_input_tokens(prompt, cached)
        input_cost = _round_div(billed, price.input_price_micro_usd, unit)
        cached_cost = _round_div(max(0, cached), price.cache_read_price_micro_usd, unit)
        output_cost = _round_div(completion, price.output_price_micro_usd, unit)
        return NormalizedCost(
            input_micro_usd=input_cost,
            cached_micro_usd=cached_cost,
            output_micro_usd=output_cost,
            price_book_id=price.price_book_id,
            unit_size=unit,
            currency=price.currency,
            fx_rate_to_usd=price.fx_rate_to_usd,
            unknown_price=False,
        )
