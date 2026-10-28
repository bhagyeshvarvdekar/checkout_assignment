POETRY ?= poetry
PYTHON ?= python3

.PHONY: install run test lint format check up down smoke package

install:
	$(POETRY) install --no-interaction

run:
	$(POETRY) run python main.py

test:
	$(POETRY) run python -m pytest -q

lint:
	$(POETRY) run ruff check .
	$(POETRY) run ruff format --check .

format:
	$(POETRY) run ruff format .

check: lint test

up:
	docker compose up --build -d --wait

down:
	docker compose down

smoke:
	$(PYTHON) scripts/smoke.py

package:
	$(PYTHON) scripts/package_submission.py
