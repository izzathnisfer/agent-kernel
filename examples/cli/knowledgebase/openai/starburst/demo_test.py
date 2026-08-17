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


@pytest.mark.order(2)
async def test_fallback_when_kb_empty(test_client):
    await test_client.send("What is the capital of France?")
    response = (test_client.last_agent_response or "").lower()
    assert "paris" in response


@pytest.mark.order(3)
async def test_kb_descriptions_exposed(test_client):
    await test_client.send("Which knowledge bases or databases do you use to store information?")
    Test.compare(test_client.last_agent_response, ["StarburstDB-mongo", "StarburstDB_Sheets"])


@pytest.mark.order(4)
async def test_kb_schemas_available(test_client):
    await test_client.send("Summarize the knowledge base schemas you know about.")
    Test.compare(test_client.last_agent_response, ["StarburstDB-mongo", "StarburstDB_Sheets", "semantic", "graph"])
