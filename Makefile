.PHONY: install run debug sandbox clean fclean lint lint-strict test

install:
	uv sync

# main.py is a placeholder until agent_mbpp / agent_swebench exist (TASKS.md sections 5-6);
# swap the target once those CLIs are real.
run:
	uv run python main.py

debug:
	uv run python -m pdb main.py

# `uv run sandbox` — interactive REPL, exit / EOF (Ctrl+D) to quit (TASKS.md section 3).
# No-op until the sandbox script entry point is wired up in pyproject.toml.
sandbox:
	uv run sandbox

clean:
	@echo "Cleaning cache and temporary files..."
	find . -type d -name "__pycache__" -not -path "./moulinette/*" -not -path "./.venv/*" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type f -name ".DS_Store" -delete
	find . -type f -name "*~" -delete
	rm -rf .mypy_cache .ruff_cache .pytest_cache .coverage htmlcov

fclean: clean
	@echo "Performing full clean..."
	rm -rf .venv

# moulinette/ is excluded: it's the extracted, gitignored copy of moulinette.zip (reference
# material, not our code) — see .gitignore.
lint:
	uv run flake8 --exclude=.venv,moulinette .
	uv run mypy --exclude moulinette --exclude .venv --exclude models.py --explicit-package-bases --warn-return-any --warn-unused-ignores --ignore-missing-imports --disallow-untyped-defs --check-untyped-defs .

lint-strict:
	uv run flake8 --exclude=.venv,moulinette .
	uv run mypy --exclude moulinette --exclude .venv --exclude models.py --strict .

test:
	uv run pytest -v
