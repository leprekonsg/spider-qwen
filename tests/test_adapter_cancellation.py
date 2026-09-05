"""Cancellation reaches the real TinyFish httpx adapter without a network call."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from spider_qwen.tools.fetch_service import TinyFishFetchProvider
from spider_qwen.tools.search_service import TinyFishSearchProvider
from spider_qwen.tools.tinyfish_client import TinyFishClient


async def _stalled_client() -> tuple[TinyFishClient, httpx.AsyncClient, asyncio.Event]:
    entered = asyncio.Event()
    never = asyncio.Event()

    async def stalled_transport(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await never.wait()
        raise AssertionError("A cancelled TinyFish request must not resume.")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(stalled_transport))
    return (
        TinyFishClient(
            "test-key", search_base_url="https://tinyfish.test/search",
            fetch_base_url="https://tinyfish.test/fetch", client=http_client,
            max_retries=2,
        ),
        http_client,
        entered,
    )


def test_search_adapter_cancels_stalled_real_httpx_transport():
    async def scenario() -> None:
        client, http_client, entered = await _stalled_client()
        provider = TinyFishSearchProvider(client)
        task = asyncio.create_task(provider.search("office cleaning", "Singapore", "en", 5))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
        finally:
            await http_client.aclose()

    asyncio.run(scenario())


def test_fetch_adapter_cancels_stalled_real_httpx_transport():
    async def scenario() -> None:
        client, http_client, entered = await _stalled_client()
        provider = TinyFishFetchProvider(client)
        task = asyncio.create_task(provider.fetch(["https://supplier.test/contact"]))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
        finally:
            await http_client.aclose()

    asyncio.run(scenario())
