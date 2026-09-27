.PHONY: setup lint types test migrate smoke run dev-bootstrap hooks version-check mutations

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

# Adversarial proof for the capability-pack trust plane (ADR 0006): every
# mutation of the trust boundary must turn the trust suite red.
mutations:
	python scripts/pack_trust_mutations.py

migrate:
	python -m nexus_ai_agent.cli migrate

smoke:
	python -m nexus_ai_agent.cli smoke "Hello, plan my day"

run:
	python -m nexus_ai_agent.cli run-bot --mode polling
