"""Browser coverage for nested viewer scrolling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_tap.viewer import render_html

playwright = pytest.importorskip("playwright.sync_api")


def _render_long_xml_viewer(tmp_path: Path) -> Path:
    xml = "<root>\n" + "\n".join(f'  <item id="{index}">value {index}</item>' for index in range(240)) + "\n</root>"
    record = {
        "timestamp": "2026-07-31T12:00:00+08:00",
        "request_id": "req_scroll_chain",
        "turn": 1,
        "duration_ms": 42,
        "request": {
            "method": "POST",
            "path": "/v1/messages",
            "headers": {},
            "body": {
                "model": "claude-test",
                "system": "\n".join(f"System prompt line {index}" for index in range(80)),
                "messages": [{"role": "user", "content": xml}],
            },
        },
        "response": {
            "status": 200,
            "headers": {},
            "body": {
                "content": [
                    {
                        "type": "text",
                        "text": "\n".join(f"Response line {index}" for index in range(160)),
                    }
                ]
            },
        },
    }
    trace = tmp_path / "trace_scroll_chain.jsonl"
    trace.write_text(json.dumps(record) + "\n", encoding="utf-8")
    output = trace.with_suffix(".html")
    assert render_html(trace, output)
    return output


def test_long_xml_releases_wheel_scroll_to_detail_at_boundaries(tmp_path: Path) -> None:
    output = _render_long_xml_viewer(tmp_path)

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {exc}")
        page = browser.new_page(viewport={"width": 1440, "height": 800})
        page.goto(output.as_uri())
        xml_block = page.locator(".content-block.text-block").first
        xml_block.wait_for()
        xml_block.scroll_into_view_if_needed()
        xml_block.hover()

        xml_block.evaluate("(element) => { element.scrollTop = element.scrollHeight; }")
        detail_before_down = page.locator("#detail").evaluate("(element) => element.scrollTop")
        page.mouse.wheel(0, 500)
        page.wait_for_timeout(100)
        detail_after_down = page.locator("#detail").evaluate("(element) => element.scrollTop")
        assert detail_after_down > detail_before_down

        xml_block.evaluate("(element) => { element.scrollTop = 0; }")
        detail_before_up = page.locator("#detail").evaluate(
            "(element) => { element.scrollTop = Math.max(element.scrollTop, 300); return element.scrollTop; }"
        )
        page.mouse.wheel(0, -500)
        page.wait_for_timeout(100)
        detail_after_up = page.locator("#detail").evaluate("(element) => element.scrollTop")
        assert detail_after_up < detail_before_up

        browser.close()
