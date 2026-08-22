.PHONY: up down logs dev test lint format typecheck migrate revision seed shell

up:
	docker compose up -d --build

dev:
	cd backend && uv run python -m app.dev

down:
	docker compose down

logs:
	docker compose logs -f

test:
	cd backend && uv run pytest

lint:
	cd backend && uv run ruff check .

format:
	cd backend && uv run ruff format .

typecheck:
	cd backend && uv run mypy app

migrate:
	cd backend && uv run alembic upgrade head

revision:
	cd backend && uv run alembic revision --autogenerate -m "$(m)"

seed:
	cd backend && uv run python -m app.db.seed

shell:
	cd backend && uv run python
