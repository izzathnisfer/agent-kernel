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
async def test_math_question(test_client):
    await test_client.send("What is 15 multiplied by 12?")
    Test.compare(test_client.last_agent_response, ["180", "15 multiplied by 12 is 180.", "The answer is 180."])


@pytest.mark.order(2)
async def test_weather_question(test_client):
    await test_client.send("What is the weather in Tokyo?")
    Test.compare(test_client.last_agent_response, ["The weather in Tokyo is sunny."])
