# Contributing

## Local setup

```bash
pip install -e ".[dev]"
python -m pytest -q
```

Use `--offline` for deterministic CLI checks:

```bash
spider-qwen run "office cleaning Singapore" --offline
spider-qwen benchmark --gold-set spider_qwen/benchmarks/gold_set.json
```

## Rules

- Keep deterministic extraction as the default path.
- Add evidence refs for any new output field.
- Do not add browser automation, form submission, code interpreter, or non-Qwen
  LLM paths.
- Do not commit `.env` or secrets.
