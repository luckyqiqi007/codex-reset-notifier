.PHONY: test check-config check-public build

test:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v

check-config:
	python3 notifier.py --config config.example.toml --check-config

check-public:
	python3 scripts/check_public.py

build:
	docker build -t codex-reset-notifier:0.1.0 .
