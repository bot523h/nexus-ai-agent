.PHONY: setup lint types test migrate smoke run

setup:
	pip install -e ".[dev]"

lint:
	ruff check . && ruff format --check .

types:
	mypy src

test:
	pytest -q -m "not slow"

migrate:
	python -m nexus_ai_agent.cli migrate

smoke:
	python -m nexus_ai_agent.cli smoke --input "Hello, plan my day"

run:
	python -m nexus_ai_agent.cli run-bot --mode polling
