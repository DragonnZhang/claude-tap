"""Shared fixtures: tmp dirs, sample records, factory helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def trace_dir(tmp_path: Path) -> Path:
    d = tmp_path / "traces"
    d.mkdir()
    return d


@pytest.fixture
def sample_anthropic_record() -> dict:
    return {
        "timestamp": "2026-05-04T10:00:00+00:00",
        "request_id": "req_test1",
        "turn": 1,
        "duration_ms": 123,
        "request": {
            "method": "POST",
            "path": "/v1/messages",
            "headers": {"x-api-key": "sk-ant-..."},
            "body": {
                "model": "claude-opus-4-6",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
        },
        "response": {
            "status": 200,
            "headers": {"content-type": "text/event-stream"},
            "body": {
                "id": "msg_1",
                "model": "claude-opus-4-6",
                "content": [{"type": "text", "text": "Hi there"}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        },
        "upstream_base_url": "https://api.anthropic.com",
    }


@pytest.fixture
def sample_jsonl(trace_dir: Path, sample_anthropic_record: dict) -> Path:
    path = trace_dir / "trace_120000.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for turn in range(1, 4):
            r = {**sample_anthropic_record, "turn": turn, "request_id": f"req_test{turn}"}
            f.write(json.dumps(r) + "\n")
    return path
