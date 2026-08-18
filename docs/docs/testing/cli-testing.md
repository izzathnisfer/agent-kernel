---
sidebar_position: 2
---

# CLI Testing

Interactive testing of CLI agents using the Agent Kernel Test framework.

## Two Independent Classes

The test framework separates two concerns into two independent classes with no relationship
to each other:

- **`CLIClient`** — drives a CLI subprocess: starts it, sends input, reads output. It knows
  nothing about comparisons or assertions.
- **`Test`** — compares an already-captured response string against expected value(s). It knows
  nothing about the CLI, subprocesses, or how the response was obtained.

You wire them together in your own test code: use `CLIClient` to talk to the CLI, then pass what
it captured into `Test.compare()`.

```python
from agentkernel.test import CLIClient, Test

# CLIClient drives the CLI subprocess
client = CLIClient("demo.py")

# Test compares strings — it doesn't care where they came from
Test.compare(actual="Paris", expected=["Paris", "The capital is Paris"], threshold=50)
```

### CLIClient Parameters
- `path`: Path to the Python CLI script (relative to current working directory)

### Test.compare() Parameters
- `actual`: The response string to check
- `expected`: A list of acceptable response strings
- `user_input`: The question that produced `actual` (used for LLM-based evaluation)
- `threshold`: Fuzzy matching threshold percentage (default: 50)
- `mode`: Primary comparison mode - any `Mode` (`Mode.FUZZY`, `Mode.JUDGE`, ...). If None, uses config value (default: None)
- `fallback_mode`: Mode run when the primary mode fails - any `Mode`. If None, uses config value; when that is unset too, no fallback runs (default: None)

## Basic Usage

### Starting a Test Session

```python
import asyncio
from agentkernel.test import CLIClient

async def run_test():
    client = CLIClient("demo.py")
    await client.start()

    # Your test interactions here

    await client.stop()

# Run the test
asyncio.run(run_test())
```

### Sending Messages and Comparing Responses

```python
from agentkernel.test import Test

# Send a message to the CLI
response = await client.send("Who won the 1996 cricket world cup?")

# Verify the response using fuzzy matching
Test.compare(client.last_agent_response, ["Sri Lanka won the 1996 cricket world cup."])
```

## Test Comparison Modes

Agent Kernel supports three comparison modes for validating responses:

### Fuzzy Mode

Uses fuzzy string matching with configurable thresholds:

```python
from agentkernel.test import Mode, Test

await client.send("Who won the 1996 cricket world cup?")
Test.compare(
    actual=client.last_agent_response,
    expected=[
        "Sri Lanka won the 1996 cricket world cup",
        "Sri Lanka won the 1996 world cup",
        "The 1996 cricket world cup was won by Sri Lanka"
    ],
    threshold=80,
    mode=Mode.FUZZY
)
```

**Note:** The `expected` parameter is a list. The test passes if the actual response fuzzy-matches **any** of the expected values above the threshold.

### Judge Mode

**Not currently implemented.** Judge mode previously used Ragas for LLM-based semantic
evaluation (`answer_similarity` against expected answers, or `answer_relevancy` against the
question when none were given); that integration has been removed. `Mode.JUDGE` currently
raises `NotImplementedError`. A replacement built on the `AKEvaluator` abstraction
(`agentkernel.test.core.akevaluators`) is planned — use `Mode.FUZZY` in the meantime.

### Fallback Mode

`fallback_mode` names another mode to fall back to when the primary `mode` fails the comparison.
Both parameters accept any mode, so any mode can lead and any mode can back it up:

```python
# Fuzzy first; if it fails, judge evaluation decides the result
await client.send("Who won the 1996 cricket world cup?")
Test.compare(
    actual=client.last_agent_response,
    expected=[
        "Sri Lanka",
        "Sri Lanka won the 1996 cricket world cup",
        "The winner was Sri Lanka"
    ],
    user_input="Who won the 1996 cricket world cup?",
    threshold=50,
    mode=Mode.FUZZY,
    fallback_mode=Mode.JUDGE
)
```

**Note:** The `expected` parameter is a list of acceptable responses. The primary mode is tried against each expected value first; only if it fails does the fallback mode run. If both fail, the raised `AssertionError` names both modes. A mode with no implementation behind it raises `NotImplementedError` rather than falling through, so a misconfigured mode isn't silently masked.

### Configuration-Based Mode

Set the default modes via a `test-config.yaml` file (in the directory the tests run from, or the path in `AK_TEST_CONFIG_PATH_OVERRIDE`) instead of passing `mode=`/`fallback_mode=` to every `Test.compare()` call. Test configuration is separate from the application's `config.yaml` and is only loaded when the test harness runs:

```yaml
# test-config.yaml
mode: fuzzy           # Primary mode: exact, fuzzy, overlap, semantic, judge, safety, structural, human
fallback_mode: judge  # Optional: mode run when the primary mode fails (omit for no fallback)
judge:
  model: gpt-4o-mini
  provider: openai
  embedding_model: text-embedding-3-small
```

```python
# Uses mode from config
await client.send("Hello")
Test.compare(client.last_agent_response, ["Hello! How can I help?"])  # Uses configured mode
```

## Advanced Features

### Custom Matching Configuration

```python
# Pass threshold and mode explicitly per comparison
Test.compare(
    actual=response,
    expected=["Expected response"],
    user_input="User question",
    threshold=70,
    mode=Mode.FUZZY
)
```

### Accessing the Latest Response

```python
await client.send("Hello!")
latest_response = client.last_agent_response  # Contains the cleaned response without ANSI codes
```

### Prompt Detection

`CLIClient` automatically detects CLI prompts using regex patterns:
- Captures prompts in format: `(agent_name) >> `
- Handles prompt changes during agent switching
- Strips ANSI escape sequences from responses

## Multi-Agent CLI Testing

For CLI applications with multiple agents:

```python
# Switch to a specific agent
await client.send("!select general")
await client.send("Who won the 1996 cricket world cup?")
Test.compare(client.last_agent_response, ["Sri Lanka won the 1996 Cricket World Cup."])

# Switch to another agent
await client.send("!select math")
await client.send("What is 2 + 2?")
Test.compare(client.last_agent_response, ["4"])
```

## Error Handling

### Assertion Errors

```python
try:
    Test.compare(client.last_agent_response, ["Expected response"])
except AssertionError as e:
    print(f"Test failed: {e}")
    # The error includes both expected and actual responses
```

### Process Management

```python
# Ensure proper cleanup even if tests fail
client = CLIClient("demo.py")
try:
    await client.start()
    # Your test code here
finally:
    await client.stop()  # Always stop the process
```

## Best Practices

### Development Testing
- Use interactive mode during development for quick validation
- Test edge cases and error conditions
- Verify agent switching functionality

### Test Mode Selection
- Use `Mode.FUZZY` for deterministic, exact outputs — it is the default primary mode
- `Mode.JUDGE` (AI-generated content with paraphrasing) is not currently implemented
- Leave `fallback_mode` unset while judge mode is unimplemented; there is no second implemented mode to fall back to yet

### Response Validation
- Use appropriate fuzzy matching thresholds (50-80% typical)
- Provide `user_input` alongside `threshold`/`mode` for forward-compatibility with judge mode once it lands
- Test with variations in expected responses
- Account for slight differences in AI model outputs

### Session Management
- Always call `client.start()` before sending messages
- Always call `client.stop()` to clean up processes
- Use try-finally blocks for proper cleanup

### Judge Mode Configuration
- Judge mode is not currently implemented (Ragas support was removed); `test-config.yaml`'s
  `judge` settings are retained for a future `AKEvaluator`-based replacement

## Example Test Session

```python
import asyncio
from agentkernel.test import CLIClient, Test

async def test_cricket_knowledge():
    client = CLIClient("demo.py")

    try:
        await client.start()

        # Test basic question - expected is a list
        await client.send("Who won the 1996 cricket world cup?")
        Test.compare(client.last_agent_response, ["Sri Lanka won the 1996 cricket world cup."], threshold=60)

        # Test follow-up question with multiple acceptable answers
        await client.send("Which country hosted the tournament?")
        Test.compare(
            client.last_agent_response,
            [
                "Co-hosted by India, Pakistan and Sri Lanka.",
                "India, Pakistan and Sri Lanka co-hosted the tournament."
            ],
            threshold=60,
        )

        print("All tests passed!")

    finally:
        await client.stop()

if __name__ == "__main__":
    asyncio.run(test_cricket_knowledge())
```

### Session Persistence

Each CLI session maintains conversation history:

```
> My name is Alice
[general] Nice to meet you, Alice!

> What's my name?
[general] Your name is Alice.
```

### Debug Mode

Enable verbose logging:

```bash
export AK_LOGGING__AK__LEVEL=DEBUG
python my_agent.py
```

### Multi-turn Conversations

Test complex interactions:

```
> I need help with a project
[general] I'd be happy to help! What's your project about?

> It's about machine learning
[general] Great! What specific aspect of machine learning?

> Image classification
[general] Image classification is a common ML task...
```

## Commands

Available CLI commands:

- `!h`, `!help`: Show help message
- `!ld`, `!load <module_name>`: Load agent module
- `!ls`, `!list`: List available agents
- `!n`, `!new`: Start a new session
- `!c`, `!clear`: Clear the current session memory
- `!s`, `!select <agent_name>`: Select an agent to run the prompt
- `!q`, `!quit`: Exit the program

## Tips

- Test edge cases interactively
- Verify agent handoffs work correctly
- Check conversation context is maintained
- Test error scenarios
- Validate tool integrations

## Example Session

```
$ python my_agent.py

AgentKernel CLI (type !help for commands or !quit to exit):
Available agents:
  research
  write
  review

(research) >> !help
Available commands:
!h, !help - Show this help message
!ld, !load <module_name> - Load agent module
!ls, !list - List available agents
!n, !new - Start a new session
!c, !clear - Clear the current session memory
!s, !select <agent_name> - Select an agent to run the prompt
!q, !quit - Exit the program

(research) >> !ls
Available agents:
  research
  write
  review

(research) >> Find information about Python
Here's what I found about Python...

(research) >> !select write
(write) >> I'll help you create a summary...

(write) >> Great, can you review it?
I'll help you create a summary of the Python information...

(write) >> !select review
(review) >> Here's my review of the content...

(review) >> !new
(review) >> This is a new session now
How can I help you in this new session?

(review) >> !quit
```
