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
from .adapters.amazon import AmazonCatalogAdapter

# 注册表
REGISTRY = {
    BoulangerCatalogAdapter.platform_name.lower(): BoulangerCatalogAdapter(),
    CurrysCatalogAdapter.platform_name.lower(): CurrysCatalogAdapter(),
    AmazonCatalogAdapter.platform_name.lower(): AmazonCatalogAdapter(),
}


def get_catalog_adapter(platform: str):
    return REGISTRY.get((platform or "").strip().lower())


def supported_catalogs():
    return [a.platform_name for a in REGISTRY.values()]
