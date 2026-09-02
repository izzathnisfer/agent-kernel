.PHONY: setup lint test demo cli api telegram verify

setup:
	uv sync --python 3.12

lint:
	uv run black --check .
	uv run isort --check-only .

test:
	uv run pytest

demo:
	uv run python demo_scenario.py

cli:
	uv run python demo.py

api:
	uv run python api_server.py

telegram:
	uv run python telegram_server.py

verify: lint test demo
