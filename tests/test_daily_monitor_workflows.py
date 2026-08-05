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
