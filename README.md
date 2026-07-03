# tvrend-scraper

欧洲电视零售渠道的**价格 / 目录抓取器**,跑在 GitHub Actions 上(公开仓库,Actions 分钟无限)。
抓取结果同步到另一个私有仓库做分析。本仓库只负责"抓取",不含任何业务字典或分析逻辑。

## 做什么

| 任务 | 频率 | 产出 |
|---|---|---|
| 价格监控 `monitor_prices` | 每天 | `raw/prices.csv`(跟踪 SKU 的当日价格) |
| 目录反向拉 `catalog_scrape` | 每周 | `catalog/*.csv`(在售商品列表快照) |

渠道:Boulanger(FR)、Currys(GB)、Elkjop(NO)。Amazon(DE) 走独立 daily catalog 链路。每个渠道一个 adapter,新增渠道只需加一个 adapter。

## 目录

```
scripts/
  monitor_prices/    每日价格抓取(Playwright + 反爬伪装)
  catalog_scrape/    每周目录抓取
mapping/
  channel_links.csv  输入:盯哪些 SKU / 对应链接(由上游私库每周刷新后推入)
raw/                 抓到的价格(滚动窗口;完整历史在私库)
catalog/             目录快照
```

## 本地运行

```bash
pip install -r requirements.txt
python -m playwright install chromium
cd scripts
python -m monitor_prices.run_daily      # 抓价格
python -m catalog_scrape.run_weekly      # 抓目录
```

环境变量:`HEADLESS_MODE`(默认 true)、`MONITOR_CONCURRENCY`(默认 3)。

## 说明

- 抓取靠伪装成普通访客读公开商品页,**不需要任何账号 / 密钥 / 凭据**。
- `mapping/channel_links.csv` 由上游维护后推入;本仓库不生成它。
