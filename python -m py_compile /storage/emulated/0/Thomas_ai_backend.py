name: Python Syntax Check

on:
  workflow_dispatch:

jobs:
  syntax:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Check Python syntax
        run: python -m py_compile Thomas_ai_backend.py
