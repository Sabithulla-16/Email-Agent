.PHONY: install lint format typecheck test test-cov run-api run-worker clean

install:
	uv venv --python 3.12
	uv pip install -e ".[dev]"

lint:
	uv run ruff check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy app/

test:
	uv run pytest -v

test-cov:
	uv run pytest --cov=app --cov-report=term-missing tests/

run-api:
	uv run uvicorn app.main:app --reload --port 8000

run-worker:
	uv run arq app.queue.worker.WorkerSettings

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".mypy_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +
