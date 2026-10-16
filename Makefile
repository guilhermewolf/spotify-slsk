# One canonical quality gate, used identically by developers and by CI
# (.github/workflows/ci.yaml calls these targets). If `make check` passes
# locally, CI should agree.
#
# Python 3.12 (see requires-python in pyproject.toml). PYTHON lets you point
# at a virtualenv: make check PYTHON=.venv/bin/python
PYTHON ?= python3.12

.PHONY: help install compile lint typecheck test audit check docker-build

help:
	@echo "make install       install runtime + dev dependencies"
	@echo "make compile       syntax-check every source file"
	@echo "make lint          ruff (rules in pyproject.toml)"
	@echo "make typecheck     mypy over the spotify_slsk package"
	@echo "make test          run the test suite"
	@echo "make audit         scan pinned dependencies for known CVEs"
	@echo "make check         compile + lint + typecheck + test + audit (the gate)"
	@echo "make docker-build  build the container image"

install:
	$(PYTHON) -m pip install -r requirements.txt -r requirements-dev.txt

compile:
	$(PYTHON) -m compileall -q spotify_slsk tests

# Rule selection and excludes live in pyproject.toml ([tool.ruff]).
lint:
	$(PYTHON) -m ruff check .

# Config in pyproject.toml ([tool.mypy]).
typecheck:
	$(PYTHON) -m mypy

# The suite is isolated by tests/conftest.py: it cannot reach a real slskd,
# a real Spotify account, or anything outside a temp directory.
test:
	$(PYTHON) -m pytest -q

audit:
	$(PYTHON) -m pip_audit -r requirements.txt --progress-spinner off

check: compile lint typecheck test audit
	@echo "quality gate passed"

docker-build:
	docker build -t spotify-slsk:local .
