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
  monitor_prices/    每日价格抓取(渠道批量快照优先 + 商品详情页兜底)
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

环境变量:`HEADLESS_MODE`(默认 true)、`MONITOR_CONCURRENCY`(默认 3)、
`MONITOR_SKU_TIMEOUT_SECONDS`(单商品总时限，默认 120 秒)、
`PLAYWRIGHT_CLOSE_TIMEOUT_SECONDS`(浏览器资源关闭时限，默认 10 秒)。

## 每日抓价策略

不同渠道使用适合本站结构的获取方式，而不是强行共用一种爬法：

| 渠道 | 主路径 | 回退路径 |
|---|---|---|
| Boulanger | 五大品牌电视 facet 批量价格快照 | 未命中 SKU 打开商品详情页 |
| Currys | 电视总类目分页快照；每页使用全新浏览器会话 | 未命中 SKU 打开商品详情页 |
| Elkjop | 站点商品动态接口 | 商品详情页 |
| Amazon | 独立的多国家 catalog 搜索链路 | 搜索补漏与详情页尺寸变体 |

Boulanger/Currys 的批量快照有两道完整性保护：商品数与跟踪清单覆盖率不足时整批作废；
价格相对历史数据发生系统性错位时整批作废。作废后自动回到原有详情页抓取，避免为了速度写入错误价格。

关联不依赖标题猜测：Currys 使用 URL 末尾商品 ID，Boulanger 使用 `/ref/<id>`，因此标题改名不会串价。

每日定时任务中，Boulanger 与 Currys 分开运行、错峰提交；一个渠道异常不会阻塞另一个渠道。
抓取过程中每完成一条就更新 `scripts/monitor_artifacts/partial_prices.csv`，Action 无论成功或失败
都会上传该检查点和调试证据，便于中断后的审计与恢复。主表 `raw/prices.csv` 仍只在整轮成功后提交。

## 说明

- 抓取读取公开分类页、公开商品接口或公开商品页，**不需要任何账号 / 密钥 / 凭据**。
- `mapping/channel_links.csv` 由上游维护后推入;本仓库不生成它。
