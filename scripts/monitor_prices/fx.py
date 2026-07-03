"""渠道价格换算为欧元的统一口径。

ECB 报价均以 1 EUR 可兑换多少单位外币表示，因此本币→EUR 使用除法。
当前跨渠道图表采用 2026-07-01 参考价：
1 EUR = 11.3125 NOK；1 EUR = 0.85973 GBP。

来源：https://www.ecb.europa.eu/stats/policy_and_exchange_rates/
      euro_reference_exchange_rates/html/index.en.html
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

ECB_RATE_DATE = "2026-07-01"
ECB_NOK_PER_EUR = Decimal("11.3125")
ECB_GBP_PER_EUR = Decimal("0.85973")

ECB_UNITS_PER_EUR = {
    "NOK": ECB_NOK_PER_EUR,
    "GBP": ECB_GBP_PER_EUR,
}


def price_to_eur(price: float | int | str | None, currency: str | None) -> float | None:
    """把已支持的本币价格换算成 EUR；未知币种返回 None，避免静默误算。"""
    if price in (None, ""):
        return None
    try:
        value = Decimal(str(price))
    except Exception:
        return None
    code = str(currency or "").upper().strip()
    if code == "EUR":
        converted = value
    elif code in ECB_UNITS_PER_EUR:
        converted = value / ECB_UNITS_PER_EUR[code]
    else:
        return None
    return float(converted.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
