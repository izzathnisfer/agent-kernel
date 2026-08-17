from agentkernel.test.core.clients.cli import CLIClient


def test_cli_client_prompt_update_and_get():
    """CLIClient owns CLI I/O concerns only (subprocess + prompt parsing)."""
    CLIClient._update_prompt("agent")
    assert CLIClient._get_prompt() == "(agent) >> "
