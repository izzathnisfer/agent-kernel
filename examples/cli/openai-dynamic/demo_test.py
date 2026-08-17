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
    await test_client.send("!select physics")
    await test_client.send("Who discovered energy emission from black holes?")
    Test.compare(test_client.last_agent_response, ["Stephen Hawking"])

    await test_client.send("!select geography")
    await test_client.send("What is the prehistoric single continent of which all current continents broke off from?")
    Test.compare(test_client.last_agent_response, ["Pangea"])

    # Selecting a non-existent agent is a no-op — the failure is logged (stderr), not printed,
    # so the previously selected 'geography' agent stays active and still answers.
    await test_client.send("!select triage")
    await test_client.send("Which ocean is the largest on Earth?")
    Test.compare(test_client.last_agent_response, ["Pacific"])
