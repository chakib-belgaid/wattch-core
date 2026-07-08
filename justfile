# Development task runner. Install: https://github.com/casey/just

default:
    just --list

build:
    cargo build --workspace

test: test-rust test-python test-ext

test-rust:
    cargo test --workspace

# Assumes a venv already exists at clients/python/.venv (see README quick start).
test-python:
    cargo build -p wattch-testd
    clients/python/.venv/bin/python -m pytest clients/python/tests

test-ext:
    cd editors/vscode-energy-tests && npm test

lint:
    cargo clippy --workspace --all-targets -- -D warnings
    cargo fmt --all --check

fmt:
    cargo fmt --all
