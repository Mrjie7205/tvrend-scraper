"""Adapter 抽象基类。"""
from __future__ import annotations


class BaseAdapter:
    """每个电商渠道一个 subclass,渠道特定的提取逻辑写在 extract_price 里。

    子类 attribute 都给默认值,只覆盖必要的。
    """

    # 必须覆盖
    platform_name: str = ""  # 用作 channel_links/prices.csv 的 Platform 值

    # 可选覆盖
    locale_override: tuple[str, str] | None = None  # (locale_str, tz_str)
    wait_selectors: tuple[str, ...] = ()  # goto 后等待价格元素
    cookie_accept_selectors: tuple[str, ...] = (
        "#onetrust-accept-btn-handler",  # OneTrust(Boulanger 等用)
    )
    # goto 前注入的 cookie(Playwright add_cookies 格式 dict 列表)。空=不注入。
    # 用于需要预置会话偏好的渠道:如 Amazon 设 lc-acbde=de_DE(德语内容)。
    context_cookies: tuple[dict, ...] = ()
    # 某些渠道的反爬验证绑定浏览器会话；同渠道 SKU 需串行复用一个 context。
    shared_context: bool = False
    # 某些渠道可以从站点 JSON/API 直接取价，优先走 direct API，失败再回退页面。
    direct_price_enabled: bool = False
    # 某些渠道可先从类目页一次性建立价格快照，命中后不再逐个打开商品详情页。
    batch_price_enabled: bool = False
    warmup_url: str | None = None
    # commit 可在重页面 DOM 尚未完成时先拿到最终 HTTP 状态/重定向地址，
    # 适合需要快速识别 404 或“200 跳分类页”的渠道。
    navigation_wait_until: str = "domcontentloaded"
    post_commit_timeout_ms: int = 15000
    antibot_max_waits: int = 4
    antibot_wait_seconds: float = 5.0

    async def extract_price_direct(self, url: str, request_context=None) -> tuple[float, str] | None:
        """可选：不打开商品页，直接从站点 API/JSON 取价。默认不支持。"""
        return None

    async def prepare_batch_prices(self, browser, skus: list[dict]) -> dict[str, tuple[float, str]]:
        """可选：返回 {batch_price_key(url): (price, currency)}；失败时返回空字典。"""
        return {}

    def batch_price_key(self, url: str) -> str:
        """批量价格表的关联键。默认使用原 URL，渠道可改用商品 ID。"""
        return (url or "").strip()

    async def extract_price(self, page) -> tuple[float, str] | None:
        """返回 (price, currency) 或 None。子类必须实现。"""
        raise NotImplementedError

    def is_dead_link(self, page_title: str) -> bool:
        """判断当前页是不是 404 / 商品下架。基类只看通用 404 关键词。"""
        t = (page_title or "").lower()
        return "404" in t or "page not found" in t or "not found" in t

    def is_unavailable_response(self, status: int, requested_url: str, final_url: str) -> bool:
        """在解析页面前识别明确失效的 HTTP 响应。"""
        return status in {404, 410}
