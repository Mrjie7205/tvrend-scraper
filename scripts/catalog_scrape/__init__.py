"""Catalog 反向拉:每周一次,抓渠道电视类目页全量列表,作为反向匹配器的输入。

设计跟 monitor_prices 一样的 adapter 插件模式:
    catalog_scrape/
        adapters/
            base.py        基类 + 通用工具
            boulanger.py   Boulanger 实现(Phase 2.1)
        run_weekly.py      主入口
        REGISTRY           注册表
"""
from __future__ import annotations

from .adapters.boulanger import BoulangerCatalogAdapter
from .adapters.currys import CurrysCatalogAdapter
from .adapters.amazon import (
    AmazonDeCatalogAdapter,
    AmazonEsCatalogAdapter,
    AmazonGbCatalogAdapter,
    AmazonItCatalogAdapter,
)
from .adapters.elkjop import ElkjopCatalogAdapter

# 注册表
REGISTRY = {
    BoulangerCatalogAdapter.platform_name.lower(): BoulangerCatalogAdapter(),
    CurrysCatalogAdapter.platform_name.lower(): CurrysCatalogAdapter(),
    "amazon_de": AmazonDeCatalogAdapter(),
    "amazon_gb": AmazonGbCatalogAdapter(),
    "amazon_it": AmazonItCatalogAdapter(),
    "amazon_es": AmazonEsCatalogAdapter(),
    ElkjopCatalogAdapter.platform_name.lower(): ElkjopCatalogAdapter(),
}


def get_catalog_adapter(platform: str):
    return REGISTRY.get((platform or "").strip().lower())


def supported_catalogs():
    return [f"{k}:{a.platform_name}/{a.country}" for k, a in REGISTRY.items()]
