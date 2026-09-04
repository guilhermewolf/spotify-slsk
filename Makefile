# One canonical quality gate, used identically by developers and by CI
# (.github/workflows/ci.yaml calls these targets). If `make check` passes
# locally, CI should agree.
#
# PYTHON lets you point at a virtualenv: make check PYTHON=.venv/bin/python
PYTHON ?= python3

.PHONY: help install compile lint test audit check docker-build

help:
	@echo "make install       install runtime + dev dependencies"
	@echo "make compile       syntax-check every source file"
	@echo "make lint          ruff (errors only, matching CI)"
	@echo "make test          run the test suite"
	@echo "make audit         scan pinned dependencies for known CVEs"
	@echo "make check         compile + lint + test + audit (the gate)"
	@echo "make docker-build  build the container image"

install:
	$(PYTHON) -m pip install -r requirements.txt -r requirements-dev.txt

compile:
	$(PYTHON) -m compileall -q app.py db.py soulseek_api.py utils.py models.py \
		log_config.py webui tests

lint:
	$(PYTHON) -m ruff check --select=E,F,W --ignore=E501 \
		--exclude .venv --exclude graphify-out .

# The suite is isolated by tests/conftest.py: it cannot reach a real slskd,
# a real Spotify account, or anything outside a temp directory.
test:
	$(PYTHON) -m pytest -q

audit:
	$(PYTHON) -m pip_audit -r requirements.txt --progress-spinner off

check: compile lint test audit
	@echo "quality gate passed"

docker-build:
	docker build -t spotify-slsk:local .
