"""渠道 adapter 注册表。

每个 adapter 提供:
- platform_name: 跟 channel_links.csv / prices.csv 的 Platform 列对齐
- locale_override: (locale, tz) 覆盖,空则按 country 走 core.locale_for
- wait_selectors: goto 后等待的 selector(任一可见即可)
- is_dead_link(page_title) -> bool: 渠道特定的死链/缺货判断
- extract_price(page) -> (price, currency) | None: 渠道特定的价格提取

加新渠道 = 新建文件 + 在 REGISTRY 里登记一行。
"""
from __future__ import annotations

from .base import BaseAdapter
from .boulanger import BoulangerAdapter
from .currys import CurrysAdapter
from .amazon import AmazonAdapter
from .elkjop import ElkjopAdapter

REGISTRY: dict[str, BaseAdapter] = {
    BoulangerAdapter.platform_name.lower(): BoulangerAdapter(),
    CurrysAdapter.platform_name.lower(): CurrysAdapter(),
    AmazonAdapter.platform_name.lower(): AmazonAdapter(),
    ElkjopAdapter.platform_name.lower(): ElkjopAdapter(),
}


def get_adapter(platform: str) -> BaseAdapter | None:
    """大小写不敏感取 adapter;不支持的渠道返回 None。"""
    return REGISTRY.get((platform or "").strip().lower())


def supported_platforms() -> list[str]:
    return [a.platform_name for a in REGISTRY.values()]
