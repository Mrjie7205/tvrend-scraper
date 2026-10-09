from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_price_monitor_workflows_are_isolated_and_bounded() -> None:
    boulanger = (ROOT / ".github" / "workflows" / "daily-monitor.yml").read_text(encoding="utf-8")
    currys = (ROOT / ".github" / "workflows" / "daily-monitor-currys.yml").read_text(encoding="utf-8")

    assert 'CHANNELS: "Boulanger"' in boulanger
    assert 'CHANNELS: "Currys"' not in boulanger
    assert 'CHANNELS: "Currys"' in currys
    assert 'CHANNELS: "Boulanger"' not in currys
    assert 'cron: "15 5 * * *"' in boulanger
    assert 'cron: "45 5 * * *"' in currys
    for content in (boulanger, currys):
        assert "timeout-minutes: 75" in content
        assert "MONITOR_SKU_TIMEOUT_SECONDS" in content
        assert "PLAYWRIGHT_CLOSE_TIMEOUT_SECONDS" in content
        assert "scripts/monitor_artifacts/" in content
        assert "if: always()" in content
        assert "python -m monitor_prices.run_daily" in content


def test_playwright_runtime_is_pinned() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "playwright==1.61.0" in requirements
    assert "playwright>=" not in requirements


def test_every_price_writer_holds_shared_queue_and_uses_run_artifacts() -> None:
    for filename in ("daily-monitor.yml", "daily-monitor-currys.yml", "daily-monitor-elkjop.yml"):
        content = (ROOT / ".github" / "workflows" / filename).read_text(encoding="utf-8")
        monitor, publish = content.split("\n  publish:", 1)
        assert "contents: read" in monitor
        assert "contents: write" not in monitor
        assert "PRICE_PUBLICATION_DIR:" in monitor
        assert "scripts/failure_artifacts/" in monitor
        assert "scripts/monitor_artifacts/" in monitor
        assert "retention-days: 14" in monitor
        assert "if-no-files-found: error" in monitor
        assert "github.run_id }}-${{ github.run_attempt" in monitor
        assert "needs: monitor" in publish
        assert "needs.monitor.result == 'success'" in publish
        assert "github.ref == 'refs/heads/main'" in publish
        assert "(inputs.max_skus || '0') == '0'" in publish
        assert "group: raw-prices-${{ github.ref }}" in publish
        assert "queue: max" in publish
        assert "cancel-in-progress: false" in publish
        assert "ref: main" in publish
        assert "python scripts/price_publication.py publish" in publish
        assert '--expected-run-id "$GITHUB_RUN_ID"' in publish
        assert '--expected-run-attempt "$GITHUB_RUN_ATTEMPT"' in publish
        assert '--expected-source-sha "$GITHUB_SHA"' in publish
        assert "-X theirs" not in content
        assert "git push" not in content
