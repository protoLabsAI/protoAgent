"""Tests for the eval webhook listener."""


import httpx
import pytest

from evals.webhook import WebhookCapture, webhook_listener


@pytest.mark.asyncio
async def test_captures_posted_json():
    async with webhook_listener() as (url, capture):
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json={"taskId": "t1", "state": "completed"},
                                  headers={"Authorization": "Bearer xyz"})
        assert r.status_code == 200
    assert len(capture.received) == 1
    assert capture.received[0] == {"taskId": "t1", "state": "completed"}
    assert capture.headers[0]["authorization"] == "Bearer xyz"


@pytest.mark.asyncio
async def test_multiple_posts_and_non_json_body():
    async with webhook_listener() as (url, capture):
        async with httpx.AsyncClient() as client:
            await client.post(url, json={"n": 1})
            await client.post(url, content=b"not json", headers={"content-type": "text/plain"})
    assert len(capture.received) == 2
    assert capture.received[0] == {"n": 1}
    assert capture.received[1]["_raw"] == "not json"


@pytest.mark.asyncio
async def test_capture_empty_until_post():
    async with webhook_listener() as (url, capture):
        assert isinstance(capture, WebhookCapture)
        assert capture.received == []
