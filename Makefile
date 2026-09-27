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

# Adversarial proof for every security boundary that ships a mutation harness:
# the pack trust plane (ADR 0006) and the restricted-shell sandbox (ADR 0011).
# Each mutation must turn its boundary's suite red; a survivor is a guard that
# exists only in prose.
mutations:
	python scripts/pack_trust_mutations.py
	python scripts/shell_sandbox_mutations.py
	python scripts/docs_and_deploy_guard_mutations.py

migrate:
	python -m nexus_ai_agent.cli migrate

smoke:
	python -m nexus_ai_agent.cli smoke "Hello, plan my day"

run:
	python -m nexus_ai_agent.cli run-bot --mode polling
