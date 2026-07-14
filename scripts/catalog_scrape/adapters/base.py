"""Catalog adapter 抽象基类。

每个渠道一个 subclass,负责:
- 提供入口 URL(可以按品牌切几个,降低单页负载)
- 翻页 / 加载更多策略
- 从 DOM 提取商品记录 (raw_name, url, size_hint?)
- 输出 list[CatalogItem]

raw 抓取数据保留尽量多的原始信息,**不在 scraper 里做匹配**(那是 下游匹配环节 的事)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


@dataclass
class CatalogItem:
    """一条从渠道类目页提取的商品记录(尚未匹配到 基础型号)。

    必填:
      brand_raw: 渠道页上显示的品牌名(可能大小写不一,匹配器会 normalize)
      raw_text:  整个商品卡片的文本(给匹配器做模糊匹配的原料)
      url:       商品详情页 URL(绝对路径,要能直接 monitor.py goto)

    可选:
      size_hint_inch: 商品标题里能看到的尺寸(如 "65 pouces" → 65),拿不到留空
      price_hint_eur: 类目页可能显示当前价(给匹配器做候选去重),不能当 prices.csv 用
    """

    brand_raw: str
    raw_text: str
    url: str
    size_hint_inch: float | None = None
    price_hint_eur: float | None = None
    price_local: float | None = None
    currency: str = ""
    price_eur: float | None = None
    extra: dict = field(default_factory=dict)  # 任何渠道特定的额外字段


class BaseCatalogAdapter:
    """子类必须设置 platform_name 和 country,实现 fetch_catalog。"""

    platform_name: str = ""
    country: str = "FR"  # 这个渠道默认服务的国家
    locale_override: tuple[str, str] | None = None
    # 某些站点会交叉校验 UA、Client Hints、操作系统和语言。设为 True 时保留
    # Chromium 自己生成的完整浏览器身份，不再套用通用随机 UA/Stealth 脚本。
    native_browser_identity: bool = False

    async def fetch_catalog(self, page) -> Sequence[CatalogItem]:
        """在 Playwright page 上抓全部商品,返回 CatalogItem 列表。"""
        raise NotImplementedError
