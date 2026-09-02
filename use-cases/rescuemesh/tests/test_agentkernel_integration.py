import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(code: str, config_name: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["AK_CONFIG_PATH_OVERRIDE"] = str(ROOT / config_name)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def test_schedule_config_injects_agentkernel_schedule_tools_into_coordinator():
    result = _run(
        """
from agentkernel.openai import OpenAIModule
from agent import AGENTS
OpenAIModule(AGENTS)
coordinator = next(agent for agent in AGENTS if agent.name == 'rescuemesh_coordinator')
names = {tool.name for tool in coordinator.tools}
required = {'create_schedule', 'list_schedules', 'get_schedule', 'update_schedule', 'delete_schedule'}
assert required <= names, (required - names, names)
print('schedule-tools-ok')
""",
        "config.schedule.yaml",
    )
    assert "schedule-tools-ok" in result.stdout


def test_telegram_config_uses_public_rescuemesh_router():
    result = _run(
        """
from agentkernel.core.config import AKConfig
config = AKConfig.get()
assert config.telegram.agent == 'rescuemesh'
assert config.telegram.api_version == 'bot'
print('telegram-config-ok')
""",
        "config.yaml",
    )
    assert "telegram-config-ok" in result.stdout
