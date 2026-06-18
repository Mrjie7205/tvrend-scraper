"""价格 CSV 读写 + 历史价格查表。

输入清单的来源(优先级):
  1) mapping/channel_links.csv  (匹配器自动维护,active=true 才跑)
  2) raw/products_seed_from_old_repo.csv  (冷启动种子,只在 channel_links 不存在时用)

输出:
  raw/prices.csv 列对齐 TV_Price_Monitor 原格式,方便后续如果要并行验证两个仓库
"""
from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Iterable

# 列结构保持跟 TV_Price_Monitor 一致,便于 4 个月历史数据无缝拼接
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

# channel_links.csv schema(匹配器产物)
CHANNEL_LINKS_COLUMNS = (
    "brand",
    "model",
    "country",
    "platform",
    "url",
    "raw_product_name",
    "confidence",
    "match_method",
    "active",
)


def _root() -> Path:
    """Channel-Prices/ 目录绝对路径。"""
    return Path(__file__).resolve().parents[2]


def channel_links_path() -> Path:
    return _root() / "mapping" / "channel_links.csv"


def prices_csv_path() -> Path:
    return _root() / "raw" / "prices.csv"


def seed_products_path() -> Path:
    return _root() / "raw" / "products_seed_from_old_repo.csv"


def load_active_skus() -> list[dict]:
    """读 channel_links.csv 里 active=true 的 SKU;不存在则回落到种子产品表。

    Returns:
        list of dicts with keys: brand, product_name, country, platform, url
    """
    out: list[dict] = []
    src = channel_links_path()
    if src.exists():
        with src.open("r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if str(row.get("active", "true")).strip().lower() not in ("true", "1", "yes"):
                    continue
                url = (row.get("url") or "").strip()
                if not url:
                    continue
                out.append({
                    "brand": (row.get("brand") or "").strip(),
                    # product_name 用于渠道展示名:优先渠道衍生码 sku(如 Boulanger 的 98C79K),
                    # 回落基础码 model。修 bug:原来只取 model → 链接显示成基础型号而非渠道叫法。
                    "product_name": ((row.get("sku") or "").strip() or (row.get("model") or "").strip()),
                    "country": (row.get("country") or "FR").strip().upper(),
                    "platform": (row.get("platform") or "").strip(),
                    "url": url,
                })
        print(f"[load] channel_links.csv → {len(out)} active SKU")
        return out

    # Fallback to seed (TV_Price_Monitor 的原 products.csv,字段大写)
    src = seed_products_path()
    if not src.exists():
        print("[load] 无 channel_links.csv,也无种子产品表,跳过本次抓取")
        return out
    with src.open("r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = (row.get("Link") or "").strip()
            if not url:
                continue
            out.append({
                "brand": (row.get("Brand") or "").strip(),
                "product_name": (row.get("Product Name") or "").strip(),
                "country": (row.get("Country") or "FR").strip().upper(),
                "platform": (row.get("Platform") or "").strip(),
                "url": url,
            })
    print(f"[load] seed products.csv → {len(out)} SKU (fallback: channel_links 还没生成)")
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
