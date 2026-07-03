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
    warmup_url: str | None = None
    antibot_max_waits: int = 4
    antibot_wait_seconds: float = 5.0

    async def extract_price(self, page) -> tuple[float, str] | None:
        """返回 (price, currency) 或 None。子类必须实现。"""
        raise NotImplementedError

    def is_dead_link(self, page_title: str) -> bool:
        """判断当前页是不是 404 / 商品下架。基类只看通用 404 关键词。"""
        t = (page_title or "").lower()
        return "404" in t or "page not found" in t or "not found" in t
