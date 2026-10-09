"""隔离保存 Amazon 未验收候选和公开字段，失败也不丢失现场。"""
from __future__ import annotations

import csv
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from failure_evidence import capture_failure, redact_text, sanitize_url


async def capture_catalog_failure(page, *, platform: str, country: str, stage: str,
                                  reason: str, adapter=None, error=None, **kwargs):
    """在真实页面关闭前留证；同一异常向上传播时复用，采集失败不改变业务结果。"""
    previous = None
    current = error
    seen = set()
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        previous = getattr(current, '_catalog_evidence_path', None)
        if previous is not None:
            break
        current = current.__cause__ or current.__context__
    if previous is not None:
        if adapter is not None:
            adapter._failure_evidence_captured = True
            adapter._failure_evidence_path = previous
        return previous
    try:
        path = await capture_failure(
            page, platform=platform, country=country, stage=stage,
            reason=reason, error=error, **kwargs,
        )
    except Exception:
        # 留证是旁路，不能把原来的超时/拒绝替换成截图或文件系统异常。
        return None
    if path is not None:
        if page is not None:
            try:
                page._catalog_failure_evidence_path = path
            except Exception:
                pass
        if isinstance(error, BaseException):
            error._catalog_evidence_path = path
        if adapter is not None:
            adapter._failure_evidence_captured = True
            adapter._failure_evidence_path = path
    return path


class AmazonCatalogDiagnostics:
    """诊断文件永不进入 catalog，未通过完整性检查的价格不能进入正式历史。"""

    CANDIDATE_COLUMNS = (
        'asin', 'brand_raw', 'raw_text', 'url', 'size_hint_inch',
        'price_local', 'currency', 'price_eur', 'country', 'observed_at',
        'validation_status',
    )
    OBSERVATION_KEYS = ('asin', 'brand', 'title', 'price', 'sizeText', 'sponsored', 'variantHint')
    PAGE_KEYS = {
        'query', 'page', 'queryKind', 'asin', 'reason', 'errorType', 'httpStatus',
        'currentUrl', 'productAsin', 'cardCount', 'nextPresent', 'nextDisabled',
        'captcha', 'robotCheck', 'continueShopping', 'deliveryRecoveryAttempted',
        'extractedRows', 'acceptedNew', 'candidateCount', 'filtered',
    }

    def __init__(self, country: str, root: Path | None = None):
        root = root or Path(os.environ.get(
            'AMAZON_DIAGNOSTICS_DIR',
            str(Path(__file__).resolve().parents[1] / 'catalog_artifacts'),
        ))
        formal = Path(__file__).resolve().parents[2] / 'catalog'
        resolved = root.resolve()
        if resolved == formal or formal in resolved.parents:
            raise ValueError('Amazon 诊断文件不能写入正式 catalog 目录')
        self.country = country
        self.observed_at = datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
        run_id = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]
        self.path = resolved / f'amazon_{country.lower()}' / run_id
        self.path.mkdir(parents=True, exist_ok=False)
        self.report = {
            'schemaVersion': 1, 'country': country, 'startedAt': self.observed_at,
            'status': 'incomplete', 'eligibleForIngestion': False,
            'notice': '诊断候选未经完整目录验收；不得作为正式目录或直接导入价格历史。',
            'pages': [], 'candidateCount': 0,
        }
        self._save_report()

    def _save_report(self) -> None:
        temp = self.path / 'report.json.tmp'
        temp.write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.path / 'report.json')

    def record_page(self, page_info: dict, rows: list[dict] | None = None) -> None:
        # 页面 URL 可能带临时令牌，只保存公开定位信息。
        safe_info = {
            key: (sanitize_url(value) if key.lower().endswith('url')
                  else redact_text(value, 500) if isinstance(value, str) else value)
            for key, value in page_info.items()
            if key in self.PAGE_KEYS
        }
        self.report['pages'].append(safe_info)
        if rows:
            # 只保存公开商品字段，不保存 HTML、Cookie、响应头、storage 或浏览器状态。
            payload = {
                'query': page_info.get('query'), 'page': page_info.get('page'),
                'queryKind': page_info.get('queryKind'),
                'observedAt': datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
                'rows': [{key: row.get(key) for key in self.OBSERVATION_KEYS} for row in rows],
            }
            with (self.path / 'search_observations.jsonl').open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + '\n')
        self._save_report()

    def checkpoint(self, items) -> None:
        items = list(items)
        temp = self.path / 'partial_candidates.csv.tmp'
        with temp.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=self.CANDIDATE_COLUMNS)
            writer.writeheader()
            for item in items:
                writer.writerow({
                    'asin': item.extra.get('asin', ''), 'brand_raw': item.brand_raw,
                    'raw_text': item.raw_text, 'url': item.url,
                    'size_hint_inch': item.size_hint_inch, 'price_local': item.price_local,
                    'currency': item.currency, 'price_eur': item.price_eur,
                    'country': self.country, 'observed_at': self.observed_at,
                    'validation_status': 'unvalidated_diagnostic_candidate',
                })
        temp.replace(self.path / 'partial_candidates.csv')
        self.report['candidateCount'] = len(items)
        self._save_report()

    def finish(self, *, status: str, reason: str | None = None, catalog_file: str | None = None) -> None:
        self.report.update(status=status, finishedAt=datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'))
        self.report['failureReason'] = redact_text(reason) if reason else reason
        self.report['validatedCatalogFile'] = catalog_file
        # 即使正式目录已通过，诊断文件也不成为另一条自动导入路径。
        self._save_report()
