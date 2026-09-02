import os
from pathlib import Path

os.environ.setdefault("AK_CONFIG_PATH_OVERRIDE", str(Path(__file__).with_name("config.schedule.yaml")))

from agentkernel.openai import OpenAIModule  # noqa: E402
from agentkernel.pipeline import IOHandler  # noqa: E402
from agentkernel.schedule import ScheduleRESTRequestHandler  # noqa: E402

from agent import AGENTS  # noqa: E402

OpenAIModule(AGENTS)

if __name__ == "__main__":
    IOHandler.run(handlers=[ScheduleRESTRequestHandler()])
