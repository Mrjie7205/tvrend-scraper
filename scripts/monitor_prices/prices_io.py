"""价格 CSV 读写 + 历史价格查表。

输入清单:mapping/channel_links.csv(6 列:brand,model,country,platform,url,active;
由上游私库匹配后推入,active=true 才抓)。

输出:raw/prices.csv;每次抓完按 Date 只保留最近 N 天(滚动窗口,N 由环境变量
PRICES_KEEP_DAYS 控制,默认 30)——完整历史由私库 enrich 留存,public 只当近窗。
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

# 价格滚动窗口:public 只保留最近这么多天(完整历史在私库)。可用 PRICES_KEEP_DAYS 覆盖。
# 45(非 30):给私库 enrich 连续失败留更宽容错垫,降低"超窗永久丢史"风险。
DEFAULT_KEEP_DAYS = 45

# 列结构保持跟 TV_Price_Monitor 一致,便于历史数据无缝拼接
PRICES_COLUMNS = (
    "Date",
    "Time",
    "Brand",
    "Product Name",
    "Country",
    "Platform",
    "Price",
    "Currency",
    "Page Title",
    "Status",
    "Price_Trend",
)


def _root() -> Path:
    """Channel-Prices/ 目录绝对路径。"""
    return Path(__file__).resolve().parents[2]


def channel_links_path() -> Path:
    return _root() / "mapping" / "channel_links.csv"


def prices_csv_path() -> Path:
    return _root() / "raw" / "prices.csv"


def load_active_skus() -> list[dict]:
    """读 channel_links.csv 里 active=true 的 SKU。channel_links 不存在则返回空 list。

    Returns:
        list of dicts with keys: brand, product_name, country, platform, url
    """
    out: list[dict] = []
    src = channel_links_path()
    if not src.exists():
        print("[load] 无 channel_links.csv,跳过本次抓取(上游私库尚未推入清单)")
        return out
    with src.open("r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if str(row.get("active", "true")).strip().lower() not in ("true", "1", "yes"):
                continue
            url = (row.get("url") or "").strip()
            if not url:
                continue
                platform = (row.get("platform") or "").strip()
                sku_name = (row.get("sku") or "").strip()
                model_name = (row.get("model") or "").strip()
                # Elkjop 的 SKU 经常把尺寸与系列拆开；使用已匹配的尺寸化 model 避免不同尺寸被聚合去重。
                product_name = model_name if platform.lower() == "elkjop" else (sku_name or model_name)
                out.append({
                    "brand": (row.get("brand") or "").strip(),
                    "product_name": product_name,
                    "country": (row.get("country") or "FR").strip().upper(),
                    "platform": platform,
                "url": url,
            })
    print(f"[load] channel_links.csv → {len(out)} active SKU")
    return out


def load_latest_historical_prices() -> dict[str, float]:
    """{name}_{country}_{platform} → 最近一次有效 Success 价格。

    用来给本轮抓到的价格打 price_trend(降价 / 涨价 / 持平 / 新上线)。

    扫描 raw/ 下所有 prices*.csv:
      - 主表 prices.csv
      - 任何归档 prices_<period>.csv(冷启动的 prices_2026_q1_q2.csv 也算)
    按文件名排序读,主表 prices.csv 最后扫(字母序 prices.csv 在 prices_2026...
    之前,但我们想让"最新"覆盖"旧"—— 所以反一下,prices_ 排在前面,prices.csv
    最后)。简化:用 mtime 排序,新文件最后扫。
    """
    out: dict[str, float] = {}
    raw_dir = _root() / "raw"
    if not raw_dir.exists():
        return out

    sources = sorted(raw_dir.glob("prices*.csv"), key=lambda p: p.stat().st_mtime)
    if not sources:
        return out

    n_total = 0
    for src in sources:
        try:
            with src.open("r", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    if row.get("Status") != "Success":
                        continue
                    p = row.get("Price")
                    if not p:
                        continue
                    try:
                        price = float(p)
                    except (TypeError, ValueError):
                        continue
                    key = f"{row.get('Product Name')}_{row.get('Country')}_{row.get('Platform')}"
                    out[key] = price  # 后写覆盖,等价于"按时间最新"
                    n_total += 1
        except Exception as e:
            print(f"[hist] {src.name} 读取异常: {e}")
            continue
    print(f"[hist] 扫了 {len(sources)} 个 prices*.csv,共 {n_total} 条 Success,"
          f"压缩成 {len(out)} 个 (model, country, platform) 的最新价")
    return out


def compute_price_trend(name: str, country: str, platform: str, new_price: float,
                        hist: dict[str, float]) -> str:
    key = f"{name}_{country}_{platform}"
    old = hist.get(key)
    if old is None:
        return "新上线"
    if new_price < old:
        return "降价"
    if new_price > old:
        return "涨价"
    return "持平"


def append_prices(rows: Iterable[dict]) -> None:
    """追加 N 行到 raw/prices.csv,首次写入自动加表头。"""
    rows = list(rows)
    if not rows:
        return
    path = prices_csv_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=PRICES_COLUMNS)
        if write_header:
            writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c, "") for c in PRICES_COLUMNS})
    print(f"[write] 追加 {len(rows)} 行 → {path.relative_to(_root().parent)}")


def trim_prices_window(keep_days: int | None = None) -> None:
    """把 raw/prices.csv 只保留最近 keep_days 天(按 Date 列 YYYY-MM-DD)。

    public 仓只当"近窗",完整历史由私库 enrich 每天并入留存——private 同步频率(每天)
    远高于本窗口(默认 30 天),且 merge 是整行去重幂等,所以裁剪不会丢数据。
    keep_days 默认读环境变量 PRICES_KEEP_DAYS,否则 30。别设太短(<14):price_trend
    需要每个 SKU 有近期价做基线。Date 解析不出的行保守保留(不误删)。
    """
    if keep_days is None:
        try:
            keep_days = int(os.environ.get("PRICES_KEEP_DAYS", DEFAULT_KEEP_DAYS))
        except (TypeError, ValueError):
            keep_days = DEFAULT_KEEP_DAYS

    path = prices_csv_path()
    if not path.exists():
        return
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or list(PRICES_COLUMNS)
    if not rows:
        return

    def _date(r):
        try:
            return datetime.strptime((r.get("Date") or "").strip(), "%Y-%m-%d").date()
        except ValueError:
            return None

    parsed = [(_date(r), r) for r in rows]
    dates = [d for d, _ in parsed if d is not None]
    if not dates:
        return  # 全部 Date 解析不出 → 不动,避免误删
    cutoff = max(dates) - timedelta(days=keep_days)
    kept = [r for d, r in parsed if d is None or d >= cutoff]  # 解析不出的保守保留
    n_drop = len(rows) - len(kept)
    if n_drop <= 0:
        return

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(kept)
    print(f"[trim] prices.csv 只留最近 {keep_days} 天(≥{cutoff}):删 {n_drop} 行,留 {len(kept)} 行")
