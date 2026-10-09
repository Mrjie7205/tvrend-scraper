"""失败现场的有界旁路采集：最小摘要先落盘，页面截图先遮挡，不改变抓取结果。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import inspect
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import uuid4
import weakref


CAPTURE_TIMEOUT_SECONDS = 4.0
MAX_SCREENSHOTS_PER_KEY = 2
MAX_SCREENSHOTS_TOTAL = 48
MAX_JSON_EVENTS = 300
MAX_ERROR_GROUPS = 64
ALLOWED_DOMAINS = ("amazon.de", "amazon.co.uk", "amazon.it", "amazon.es", "amazon.fr",
                   "boulanger.com", "currys.co.uk", "elkjop.no")
_STATES: dict[str, dict] = {}
_PAGES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_LOCK = threading.RLock()
_MASK_ATTRIBUTE = "data-tvrend-failure-mask"
_MASK_SELECTORS = (
    f"[{_MASK_ATTRIBUTE}],input,textarea,select,[contenteditable],[autocomplete],address,iframe,canvas,video,"
    '[id*="account" i],[class*="account" i],[id*="address" i],[class*="address" i],'
    '[id*="profile" i],[class*="profile" i],[id*="customer" i],[class*="customer" i],'
    '[id*="postcode" i],[class*="postcode" i],[id*="postal" i],[class*="postal" i],'
    '[id*="recipient" i],[class*="recipient" i],[id*="glow" i],[id*="delivery" i],'
    '[class*="delivery" i],[id*="greeting" i],[class*="greeting" i],[id*="username" i]'
)

# 不取 body 全文、HTML、表单值或隐藏字段。先标记个人区域，随后 screenshot 的 mask 覆盖这些节点。
_VISIBLE_STRUCTURE_JS = r"""() => {
  const attr = 'data-tvrend-failure-mask';
  const visible = el => !!(el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
  const sensitive = 'input,textarea,select,[contenteditable],iframe,canvas,video,address,' +
    '[autocomplete],[id*="account" i],[class*="account" i],[id*="address" i],[class*="address" i],' +
    '[id*="profile" i],[class*="profile" i],[id*="customer" i],[class*="customer" i],' +
    '[id*="postcode" i],[class*="postcode" i],[id*="postal" i],[class*="postal" i],' +
    '[id*="recipient" i],[class*="recipient" i],[id*="glow" i],[id*="delivery" i],' +
    '[class*="delivery" i],[id*="greeting" i],[class*="greeting" i],[id*="username" i]';
  let masked = 0;
  const mark = el => { if (!el.hasAttribute(attr)) { el.setAttribute(attr, '1'); masked++; } };
  document.querySelectorAll(sensitive).forEach(mark);
  const personal = /[\w.+-]+@[\w.-]+\.[a-z]{2,}|\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b|\b\d{5}(?:-\d{4})?\b|\+?\d[\d ()-]{7,}\d|\b(?:hello|bonjour|hallo|deliver to|livrer|lieferadresse|address|adresse|indirizzo|direcci[oó]n|recipient|customer name)\b|\b\d{1,5}\s+(?:[\w'-]+\s+){0,4}(?:street|road|avenue|lane|drive|rue|strasse|straße|calle|via)\b/i;
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  let node, checked = 0;
  while ((node = walker.nextNode()) && checked++ < 12000) {
    const parent = node.parentElement;
    if (parent && !['SCRIPT','STYLE','NOSCRIPT'].includes(parent.tagName) && visible(parent) && personal.test(node.textContent || '')) mark(parent);
  }
  // 未完成全页检查时拒绝截图，不能把遍历上限变成脱敏漏洞。
  const ready = !node;
  const publicText = el => el.closest('[' + attr + ']') ? '[redacted]' : (el.innerText || '').trim().slice(0, 160);
  const headings = [...document.querySelectorAll('h1,h2,[role="alert"]')].filter(visible).slice(0, 16)
    .map(el => ({tag: el.tagName.toLowerCase(), text: publicText(el)}));
  const buttons = [...document.querySelectorAll('button,a,[role="button"],input[type="submit"]')].filter(visible).slice(0, 30)
    .map(el => ({label: publicText(el), url: el.href || el.formAction || ''}));
  return {redaction_ready: ready, masked_elements: masked, title: document.title,
          headings, buttons, form_count: document.forms.length, input_count: document.querySelectorAll('input,textarea,select').length};
}"""


def sanitize_url(value) -> str | None:
    """仅保留已知零售域的 origin/path；令牌、账户路径和全部查询参数均不落盘。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value.strip())
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS):
            return None
        path = unquote(parsed.path or "/")
        if re.search(r"(?i)(?:account|address|checkout|orders?|signin|login|profile|token|session|password|secret|signature|api[_-]?key|[?&#=@%])", path):
            path = "/[redacted]"
        path = re.sub(r"[\x00-\x20\x7f]", "_", path)[:300]
        path = re.sub(r"[A-Za-z0-9_-]{80,}", "[redacted]", path)
        return urlunsplit((parsed.scheme, host, path, "", ""))
    except (TypeError, ValueError):
        return None


def redact_text(value, limit: int = 240) -> str:
    """错误与公开结构使用同一脱敏规则；敏感语境整段省略，宁缺勿泄漏。"""
    text = re.sub(r"\s+", " ", str(value or ""))[:2000]
    text = re.sub(r"https?://[^\s<>\"']+", lambda m: sanitize_url(m.group()) or "[redacted-url]", text)
    if re.search(r"(?i)\b(?:cookies?|headers?|authorization|bearer|token|secret|password|storage(?:state)?|address|adresse|indirizzo|postcode|postal|recipient|deliver to|hello|bonjour|hallo|hola|ciao)\b|(?:api|access|auth|csrf|session)[_-]?(?:key|token|secret|id)|signature\s*[:=]", text):
        return "[redacted-sensitive-text]"
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[redacted-email]", text)
    text = re.sub(r"(?i)\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", "[redacted-postcode]", text)
    text = re.sub(r"(?<!\w)\+?\d[\d ()-]{7,}\d(?!\w)", "[redacted-phone]", text)
    text = re.sub(r"\b\d{5}(?:-\d{4})?\b", "[redacted-postcode]", text)
    text = re.sub(r"(?i)\b\d{1,5}\s+(?:[\w'-]+\s+){0,4}(?:street|road|avenue|lane|drive|rue|strasse|straße|calle|via)\b", "[redacted-address]", text)
    return text[:max(0, min(limit, 500))]


def _code(value, fallback="unknown") -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", redact_text(value, 80)).strip("_")[:64] or fallback


def _machine_id(name: str, pattern: str, fallback: str) -> str:
    """GitHub 机器标识不是电话号码；严格验证允许格式后保留原值。"""
    value = os.environ.get(name, "")
    return value if re.fullmatch(pattern, value) else fallback


def _root() -> Path:
    return Path(os.environ.get("FAILURE_EVIDENCE_DIR", str(Path(__file__).resolve().parent / "failure_artifacts"))).resolve()


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix("." + uuid4().hex[:8] + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _state(root: Path) -> dict:
    return _STATES.setdefault(str(root), {"events": 0, "screenshots": 0, "screenshot_attempts": 0, "duplicates": 0, "groups": {}})


def _summary(root: Path, state: dict) -> None:
    _atomic_json(root / "summary.json", {"schema_version": 1, "events": state["events"],
                 "screenshots": state["screenshots"], "duplicate_captures": state["duplicates"],
                 "screenshot_attempts": state["screenshot_attempts"],
                 "groups": list(state["groups"].values())})


def _record(*, platform, country, stage, reason, url=None, http_status=None, product=None,
            error=None, browser_available=False):
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    key = (_code(platform), _code(country), _code(reason))
    with _LOCK:
        state = _state(root)
        if key not in state["groups"] and len(state["groups"]) >= MAX_ERROR_GROUPS:
            key = ("other", "other", "other")
        group = state["groups"].setdefault(key, {"platform": key[0], "country": key[1], "reason": key[2],
                      "events": 0, "screenshots": 0, "screenshot_attempts": 0, "screenshots_omitted": 0, "json_omitted": 0})
        state["events"] += 1
        group["events"] += 1
        if state["events"] > MAX_JSON_EVENTS and group["events"] > 1:
            group["json_omitted"] += 1
            _summary(root, state)
            return None
        identifier = uuid4().hex
        path = root / ("_".join(key) + "_" + identifier[:12] + ".json")
        if isinstance(product, dict):
            product = product.get("product_name") or product.get("model") or product.get("asin")
        document = {"schema_version": 1, "event_id": identifier,
                    "utc_time": datetime.now(timezone.utc).isoformat(),
                    "run_id": _machine_id("GITHUB_RUN_ID", r"[1-9]\d{0,19}", "local"),
                    "run_attempt": _machine_id("GITHUB_RUN_ATTEMPT", r"[1-9]\d{0,5}", "1"),
                    "head_sha": _machine_id("GITHUB_SHA", r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})", "unknown"),
                    "platform": key[0], "country": key[1], "stage": _code(stage), "reason": key[2],
                    "url": sanitize_url(url), "product": redact_text(product, 100),
                    "http_status": http_status if isinstance(http_status, int) and 0 <= http_status <= 599 else None,
                    "browser_available": bool(browser_available),
                    "error": {"type": type(error).__name__, "message": redact_text(error)} if error else None,
                    "visible_structure": None, "screenshot": {"status": "not_attempted", "file": None}}
        _atomic_json(path, document)
        _summary(root, state)
        return root, state, group, path, document


def record_failure(*, platform, country, stage, reason, url=None, http_status=None, product=None,
                   error=None, browser_available=False) -> Path | None:
    """没有页面时也先留最小 JSON；目录/磁盘失败仅放弃诊断，不改变主任务。"""
    try:
        result = _record(platform=platform, country=country, stage=stage, reason=reason, url=url,
                         http_status=http_status, product=product, error=error, browser_available=browser_available)
        return result[3] if result else None
    except Exception:
        return None


def _consume(task) -> None:
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


async def _bounded(awaitable, seconds: float):
    """超时后发出取消，不等待浏览器清理确认，避免 wait_for 的取消等待扩大诊断预算。"""
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=max(0, seconds))
        if not done:
            raise asyncio.TimeoutError()
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            task.add_done_callback(_consume)


def _visible_payload(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {"title": redact_text(raw.get("title")),
            "headings": [{"tag": _code(x.get("tag")), "text": redact_text(x.get("text"))}
                         for x in raw.get("headings", [])[:16] if isinstance(x, dict)],
            "buttons": [{"label": redact_text(x.get("label"), 100), "url": sanitize_url(x.get("url"))}
                        for x in raw.get("buttons", [])[:30] if isinstance(x, dict)],
            "form_count": min(100, max(0, int(raw.get("form_count", 0)))),
            "input_count": min(1000, max(0, int(raw.get("input_count", 0)))),
            "masked_elements": min(12000, max(0, int(raw.get("masked_elements", 0))))}


async def capture_failure(page, *, platform, country, stage, reason, url=None, http_status=None,
                          product=None, error=None, timeout_seconds=None) -> Path | None:
    """先持久化最小现场，再限时采集已脱敏结构/真实页面截图；任何采集异常均旁路处理。"""
    path = None
    document = None
    try:
        root = _root()
        try:
            current_url = getattr(page, "url", None) if page is not None else None
        except Exception:
            current_url = None
        target_url = current_url if isinstance(current_url, str) and current_url.startswith("http") else url
        # URL 查询串不落盘；内存指纹仍区分同一搜索路径上的不同品牌/分页目标。
        fingerprint = hashlib.sha256(str(target_url or "").encode()).hexdigest()
        key = (str(root), _code(platform), _code(country), _code(stage), _code(reason),
               sanitize_url(target_url), redact_text(product, 100), fingerprint)
        if page is not None:
            try:
                previous = _PAGES.get(page, {}).get(key)
                if previous and previous.exists():
                    with _LOCK:
                        state = _state(root)
                        state["duplicates"] += 1
                        _summary(root, state)
                    return previous
            except (TypeError, AttributeError):
                pass
        result = _record(platform=platform, country=country, stage=stage, reason=reason,
                         url=target_url, http_status=http_status, product=product, error=error,
                         browser_available=page is not None)
        if result is None:
            return None
        root, state, group, path, document = result
        available = page is not None
        if available and callable(getattr(page, "is_closed", None)):
            closed = page.is_closed()
            if inspect.iscoroutine(closed):
                closed.close()  # 兼容测试替身，不把未 await 的探测协程遗留到清理阶段。
            else:
                available = closed is not True
        document["browser_available"] = available
        if page is not None:
            try:
                _PAGES.setdefault(page, {})[key] = path
            except TypeError:
                pass
        if not available:
            document["screenshot"]["status"] = "page_unavailable"
            return path
        with _LOCK:
            if group["screenshot_attempts"] >= MAX_SCREENSHOTS_PER_KEY or state["screenshot_attempts"] >= MAX_SCREENSHOTS_TOTAL:
                group["screenshots_omitted"] += 1
                document["screenshot"]["status"] = "omitted_limit"
                _summary(root, state)
                return path
            group["screenshot_attempts"] += 1
            state["screenshot_attempts"] += 1
            _summary(root, state)
        seconds = CAPTURE_TIMEOUT_SECONDS if timeout_seconds is None else min(CAPTURE_TIMEOUT_SECONDS, max(0.01, float(timeout_seconds)))
        deadline = asyncio.get_running_loop().time() + seconds
        raw = await _bounded(page.evaluate(_VISIBLE_STRUCTURE_JS), min(1.5, seconds))
        document["visible_structure"] = _visible_payload(raw)
        if not isinstance(raw, dict) or raw.get("redaction_ready") is not True:
            document["screenshot"]["status"] = "redaction_unavailable"
            return path
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError()
        screenshot = await _bounded(page.screenshot(full_page=False, animations="disabled", mask_color="#000000",
                         mask=[page.locator(_MASK_SELECTORS)], timeout=max(1, int(remaining * 1000))), remaining)
        if not isinstance(screenshot, bytes) or not screenshot.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("screenshot_not_png")
        image_path = path.with_suffix(".png")
        image_path.write_bytes(screenshot)
        document["screenshot"] = {"status": "saved", "file": image_path.name, "redacted": True}
        with _LOCK:
            group["screenshots"] += 1
            state["screenshots"] += 1
            _summary(root, state)
        return path
    except (Exception, asyncio.CancelledError) as exc:
        if document is not None:
            document["screenshot"]["status"] = "capture_timeout" if isinstance(exc, asyncio.TimeoutError) else "capture_failed"
            document["capture_error_type"] = type(exc).__name__
        return path
    finally:
        if path is not None and document is not None:
            try:
                _atomic_json(path, document)
            except Exception:
                pass  # 最初的最小 JSON 已经落盘，最终更新失败不能覆盖主抓取异常。
