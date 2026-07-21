"""Agent smoke test — requires a live model key.

Uses the ``Test("demo.py")`` harness, which starts the real agents. The default
agent is the read-only Consumer, which answers a question grounded in the
committed ``sample_bundle/`` (no S3 sync needed). Skipped when no OpenAI key is
present, matching how the other CLI examples behave in CI.
"""

import os

import pytest
import pytest_asyncio
from agentkernel.test import Test

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="OPENAI_API_KEY not set"),
]


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def test_client():
    test = Test("demo.py")
    await test.start()
    try:
        yield test
    finally:
        await test.stop()


@pytest.mark.order(1)
async def test_consumer_answers_from_bundle(test_client):
    await test_client.send("What is the grain of the orders table? Answer in one short sentence.")
    # Assert containment on the raw response: a short grounded keyword is more
    # robust than whole-string fuzzy matching for a smoke test.
    response = (test_client.last_agent_response or "").lower()
    assert "one row per" in response


@pytest.mark.order(2)
async def test_consumer_follows_relationships(test_client):
    await test_client.send("Which table is the Monthly Revenue metric derived from?")
    response = (test_client.last_agent_response or "").lower()
    assert "orders" in response
