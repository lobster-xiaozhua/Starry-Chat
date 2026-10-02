.PHONY: test test-cov test-fast audit

# 运行全部测试（带覆盖率报告）
test:
	pytest -x -v --tb=short --cov=app --cov-report=term-missing

# 仅运行（不强制覆盖率）
test-fast:
	pytest -x -v --tb=short

# 依赖漏洞扫描（需先 pip install pip-audit）
audit:
	pip-audit -r requirements.txt

# 本地开发启动（reload 开启）
dev:
	python run.py
