.PHONY: setup lint types test migrate smoke run dev-bootstrap runtime-check runtime-receipt hooks version-check mutations

setup:
	pip install -e ".[dev]"

dev-bootstrap:
	bash scripts/bootstrap_dev.sh

runtime-check:
	python scripts/check_runtime.py

runtime-receipt:
	python scripts/runtime_receipt.py

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

mutations:
	python scripts/pack_trust_mutations.py
	python scripts/security_mutations_remote_key.py

migrate:
	python -m nexus_ai_agent.cli migrate

smoke:
	python -m nexus_ai_agent.cli smoke "Hello, plan my day"

run:
	python -m nexus_ai_agent.cli run-bot --mode polling
