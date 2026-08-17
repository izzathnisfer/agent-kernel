import asyncio
import re
import sys
from pathlib import Path


class CLIClient:
    """Drives a CLI subprocess under test: starts it, sends input, and reads output up to its prompt."""

    _prompt_regex = re.compile(r"\((.+?)\) >> $")  # captures terminal prompt
    _prompt: str = ""

    def __init__(self, path):
        """
        Initializes a CLI client for a specified command-line interface (CLI) path.
        :param path: Python file path as a string
        """
        working_dir = Path.cwd()
        self.path = working_dir / path
        self.proc = None
        self.last_agent_response = None
        self.last_user_input = ""
        self._stderr_task = None

    @classmethod
    def _update_prompt(cls, text: str):
        """
        Updates the global prompt string.
        :param text: The text to be inserted into the global prompt.
        """
        cls._prompt = f"({text}) >> "

    @classmethod
    def _get_prompt(cls):
        """
        Returns the global prompt string.
        """
        return cls._prompt

    async def _read_until_prompt(self):
        """
        Reads from the subprocess stdout until the prompt is found.
        """
        if self.proc is None:
            raise RuntimeError("Process not started")
        output_bytes = b""
        captured_prompt_text = None

        while True:
            chunk = await self.proc.stdout.read(1024)
            if not chunk:
                break
            output_bytes += chunk
            try:
                output_str = output_bytes.decode("utf-8")
            except UnicodeDecodeError:
                continue  # wait for more bytes if multibyte char is incomplete

            # Search for prompt at the end
            match = self._prompt_regex.search(output_str[-30:])
            if match:
                captured_prompt_text = match.group(1)
                return output_str, captured_prompt_text

        return output_bytes.decode("utf-8"), captured_prompt_text

    async def start(self):
        """
        Starts the CLI to initialize the test
        """
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable,
            self.path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            # Keep stderr separate: agent responses are stdout-only, while log output
            # (AK loggers write to stderr) would otherwise pollute captured responses
            # and break comparisons. It is drained in the background for diagnostics.
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.get_running_loop().create_task(self._drain_stderr())

        # Capture the initial welcome message and prompt
        welcome, prompt_text = await self._read_until_prompt()
        welcome_stripped = self._prompt_regex.sub("", welcome).strip()
        print(welcome_stripped, flush=True)
        self._update_prompt(prompt_text)

    async def send(self, message: str) -> str:
        """
        Sends a message to the CLI and returns the response.
        :param message: The message to be sent to the CLI.
        :return: The response from the subprocess.
        """
        print(f"{self._get_prompt()}{message}", flush=True)
        self.last_user_input = message
        self.proc.stdin.write((message + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

        output, prompt_text = await self._read_until_prompt()
        # Remove the prompt from the end
        response = self._prompt_regex.sub("", output).strip()
        print(response, flush=True)
        self._update_prompt(prompt_text)
        ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
        self.last_agent_response = ansi_escape.sub("", response)
        return self.last_agent_response

    async def _drain_stderr(self):
        """
        Continuously drains the CLI's stderr (log output), echoing each line to the test
        runner's stderr. Keeps the pipe from filling (which would block the subprocess)
        while keeping logs out of the captured agent responses.
        """
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            print(line.decode("utf-8", errors="replace").rstrip(), file=sys.stderr, flush=True)

    async def stop(self):
        """
        Stops the CLI.
        """
        self.proc.stdin.close()
        await self.proc.wait()
        if self._stderr_task is not None:
            await self._stderr_task  # finishes on stderr EOF once the process exits
            self._stderr_task = None
