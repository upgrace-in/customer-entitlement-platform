PYTHON := $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)

.PHONY: install run test test-contract mcp-server demo clean

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev]"

run:
	$(PYTHON) -m uvicorn src.main:app --reload --host 0.0.0.0 --port 8000

test:
	$(PYTHON) -m pytest tests/ -v --cov=src --cov-report=term-missing

test-contract:
	$(PYTHON) -m pytest tests/contract/ -v

mcp-server:
	$(PYTHON) mcp/server.py

demo:
	$(PYTHON) mcp/demo_transcript.py

clean:
	rm -rf .pytest_cache __pycache__ *.db .coverage
	find . -name "__pycache__" -type d -exec rm -rf {} +
