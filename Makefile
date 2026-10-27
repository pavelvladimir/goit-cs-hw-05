.DEFAULT_GOAL := help
export UV_CACHE_DIR ?= $(CURDIR)/.cache/uv
export MPLCONFIGDIR ?= $(CURDIR)/.cache/matplotlib
ROCKY_TEST_IMAGE ?= goit-cs-hw-05-rocky-tests
.PHONY: help init sync demo validate-reports benchmark lint lint-python lint-shell lint-docs format typecheck test coverage check build-rocky-tests check-rocky audit

help: ## Показати доступні команди
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

init: sync ## Підготувати локальне середовище

sync: ## Встановити залежності Python 3.14 з lockfile
	uv sync --locked

demo: ## Показати обидва завдання без зовнішньої мережі
	uv run --locked python -m scripts.demo

validate-reports: ## Перевірити JSON-звіти попереднього make demo за схемою v1
	uv run --locked python -m scripts.validate_report artifacts/demo/sorter.json artifacts/demo/words.json

benchmark: ## Виміряти копіювання та MapReduce; перевірити SHA-256 і частоти
	uv run --locked python -m scripts.benchmark

lint: lint-python lint-shell lint-docs ## Перевірити Python, Bash і документацію

lint-python: ## Перевірити Ruff і форматування
	uv run --locked ruff check --output-format concise .
	uv run --locked ruff format --check .

lint-docs: ## Перевірити Markdown
	uv run --locked pymarkdown --config pyproject.toml scan README.md docs/*.md

lint-shell: ## Перевірити синтаксис локального Docker-runner
	bash -n docker/check-rocky.sh

format: ## Форматувати Python
	uv run --locked ruff format .

typecheck: ## Перевірити strict mypy
	uv run --locked mypy --show-column-numbers --no-pretty --no-color-output

test: ## Виконати тести з локальним HTTP-сервером
	uv run --locked pytest

coverage: ## Виконати тести з branch coverage >= 90%
	uv run --locked coverage run -m pytest
	uv run --locked coverage report
	uv run --locked coverage html
	uv run --locked coverage xml

check: lint typecheck coverage ## Виконати всі локальні перевірки

build-rocky-tests: ## Зібрати локальний тестовий образ Rocky Linux
	docker build --file docker/tests.Dockerfile --tag "$(ROCKY_TEST_IMAGE)" .

check-rocky: ## Перевірити код, тести, coverage та demo в Rocky Linux
	bash docker/check-rocky.sh "$(ROCKY_TEST_IMAGE)"

audit: ## Перевірити залежності за базою вразливостей; потрібна мережа
	@mkdir -p artifacts
	uv export --locked --all-groups --format requirements-txt --output-file artifacts/audit-requirements.txt --quiet
	uv run --locked pip-audit --cache-dir .cache/pip-audit --require-hashes --disable-pip --progress-spinner off -r artifacts/audit-requirements.txt
