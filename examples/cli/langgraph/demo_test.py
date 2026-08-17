import pytest
import pytest_asyncio
from agentkernel.test import CLIClient, Test

pytestmark = pytest.mark.asyncio(loop_scope="session")  # uses a single session for all tests


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def test_client():
    client = CLIClient("demo.py")
    await client.start()
    try:
        yield client
    finally:
        await client.stop()


@pytest.mark.order(1)
async def test_first_question(test_client):
    await test_client.send("!select general")
    await test_client.send("Who won the 1996 cricket world cup?")
    Test.compare(test_client.last_agent_response, ["Sri Lanka won the 1996 Cricket World Cup."])


@pytest.mark.order(2)
async def test_follow_up_question(test_client):
    await test_client.send("Which country hosted the tournament?")
    Test.compare(test_client.last_agent_response, ["Co-hosted by India, Pakistan and Sri Lanka."])
