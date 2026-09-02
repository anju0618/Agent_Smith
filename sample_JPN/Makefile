.PHONY: install run debug sandbox clean fclean lint lint-strict test

install:
	uv sync

# No single canonical entry point (this project exposes agent_mbpp, agent_swebench,
# and the sandbox CLI) - `run` demonstrates the MBPP agent's CLI; see README.md for
# the full dump -> run -> validate workflow against a real task.
run:
	uv run python -m agent_mbpp --help

debug:
	uv run python -m pdb -m agent_mbpp --help

# `uv run sandbox` - interactive REPL, exit / EOF (Ctrl+D) to quit.
sandbox:
	uv run sandbox

clean:
	find . -type d -name "__pycache__" -not -path "./.venv/*" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	rm -rf .mypy_cache .pytest_cache .coverage htmlcov

fclean: clean
	rm -rf .venv

lint:
	uv run flake8 --exclude=.venv .
	uv run mypy --exclude .venv --exclude models.py --explicit-package-bases \
		--warn-return-any --warn-unused-ignores --ignore-missing-imports \
		--disallow-untyped-defs --check-untyped-defs .

lint-strict:
	uv run flake8 --exclude=.venv .
	uv run mypy --exclude .venv --exclude models.py --strict .

test:
	uv run pytest -v
