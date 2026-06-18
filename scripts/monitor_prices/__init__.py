"""Channel price monitor — daily Playwright-driven price grab.

入口 run_daily.py 从 channel_links.csv(active=true 的 SKU)读 URL 清单,
按 platform 分发到对应 adapter,抓商品页价格,追加进 raw/prices.csv。

代码 port 自 TV_Price_Monitor/monitor.py(2026-02 → 2026-06 稳定运行 4 个月)
重构成 adapter 插件式架构:加新渠道 = 在 adapters/ 下新增一个文件 + 在
ADAPTERS 字典登记,不动主流程。
"""
