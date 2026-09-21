.PHONY: setup lint types test migrate smoke run dev-bootstrap hooks version-check

setup:
	pip install -e ".[dev]"

dev-bootstrap:
	bash scripts/bootstrap_dev.sh

hooks:
	pip install pre-commit && pre-commit install --install-hooks

version-check:
	python scripts/check_version_lockstep.py

lint:
	ruff check . && ruff format --check .

types:
	mypy src

test:
	pytest -q -m "not slow"

migrate:
	python -m nexus_ai_agent.cli migrate

smoke:
	python -m nexus_ai_agent.cli smoke "Hello, plan my day"

run:
	python -m nexus_ai_agent.cli run-bot --mode polling
