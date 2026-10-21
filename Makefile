.PHONY: clean build serve test install install-dev venv

PORT ?= 8000

# ============================================================================ #
# CLEAN COMMANDS
# ============================================================================ #

clean: clean-build clean-pyc ## remove all build, test, coverage and Python artifacts

clean-build: ## remove build artifacts
	rm -fr build/
	rm -fr dist/
	rm -fr .eggs/
	mkdir -p build/

clean-pyc: ## remove Python file artifacts
	find . -name '*.pyc' -exec rm -f {} +
	find . -name '*.pyo' -exec rm -f {} +
	find . -name '*~' -exec rm -f {} +
	find . -name '__pycache__' -exec rm -fr {} +
# ============================================================================ #
# INSTALL COMMANDS
# ============================================================================ #

venv:
	uv venv --clear --python python3.14

install: venv
	uv sync

install-dev: venv
	uv sync --dev

# ============================================================================ #
# BUILD COMMANDS
# ============================================================================ #

# build: clean
build:
	uv run main.py

test: ## run the offline regression suite
	uv run --frozen python -m unittest discover -v

serve: ## serve the generated index locally
	uv run python -m http.server $(PORT) --bind 127.0.0.1 --directory build
