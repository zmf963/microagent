---
kind: build_system
name: Python 包构建与 CI 流水线
category: build_system
scope:
    - '**'
source_files:
    - Makefile
    - pyproject.toml
    - .github/workflows/ci.yml
    - uv.lock
---

本项目采用基于 Hatchling 的 Python 包构建系统，配合 uv 作为依赖管理与执行器，通过 Makefile 统一编排 lint、测试、覆盖率与打包流程，并由 GitHub Actions 驱动 CI。

### 构建工具链
- 构建后端：hatchling.build（pyproject.toml 中声明），wheel 包输出目录为 src/microagent。
- 依赖管理：使用 uv（astral-sh/setup-uv@v5）进行环境同步与命令执行，uv.lock 锁定版本。
- 可选功能集：通过 [project.optional-dependencies] 定义 dev、mcp、cron、ssh、browser 五组可选依赖，按需安装。
- CLI 入口：microagent = "microagent.surface.cli:main" 注册为可执行脚本。

### Makefile 目标
- test：运行 tests/unit/ 下的单元测试（默认）。
- ci：同时运行 unit 与 integration 测试。
- lint / fix：调用 Ruff 检查并自动修复源码与测试。
- cov：使用 coverage 生成覆盖率报告。
- build：执行 uv build 生成 wheel/sdist。
- clean：清理 __pycache__、.pytest_cache、*.egg-info、dist/、build/、.coverage、htmlcov/。

### CI 流水线（.github/workflows/ci.yml）
- 触发条件：push/PR 到 main 分支。
- 矩阵策略：仅使用 Python 3.14。
- 步骤：checkout → setup-uv → uv sync --group dev → make lint || true → make test → make build。

### 代码质量配置
- Ruff：line-length=100，target-version=py314，启用 E/F/I/W/UP/B/C4/SIM 规则集，排除 tests/integration/ 等路径。
- Pytest：asyncio_mode=auto，标记 integration 用于区分需真实 LLM API 的端到端测试。
- Coverage：source 限定 src/microagent，omit tests，fail_under=0（不强制通过率）。

### 开发者约定
- 新增可选功能需在 [project.optional-dependencies] 中声明对应分组。
- 所有 lint/format 规则由 Ruff 统一管理，提交前应运行 make fix。
- 单元测试放 tests/unit/，集成测试放 tests/integration/ 并用 @pytest.mark.integration 标记。
- 构建产物统一输出至 dist/，本地开发通过 uv run microagent 调用 CLI。