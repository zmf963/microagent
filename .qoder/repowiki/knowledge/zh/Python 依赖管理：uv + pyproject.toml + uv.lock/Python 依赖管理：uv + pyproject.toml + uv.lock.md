---
kind: dependency_management
name: Python 依赖管理：uv + pyproject.toml + uv.lock
category: dependency_management
scope:
    - '**'
source_files:
    - pyproject.toml
    - uv.lock
    - Makefile
    - .github/workflows/ci.yml
---

## 1. 使用的系统与工具
- **包管理器**：`uv`（Rust 实现的高性能 Python 包管理器），用于安装、解析和锁定依赖。
- **元数据声明**：`pyproject.toml`，使用 PEP 621 格式集中声明运行时依赖、可选依赖与构建后端（hatchling）。
- **依赖锁定**：`uv.lock`，记录所有直接/间接依赖的精确版本、来源（PyPI）与哈希值，保证可重复构建。
- **虚拟环境**：`.venv/`，通过 `uv` 创建并隔离开发依赖。
- **CI 集成**：`.github/workflows/ci.yml` 与 `Makefile` 串联 lint、测试、构建流程。

## 2. 关键文件
- `pyproject.toml` — 项目元数据、依赖声明、可选功能集（dev/mcp/cron/ssh/browser）、CLI 入口、pytest/ruff/coverage 配置。
- `uv.lock` — 完整依赖树锁定文件，包含每个包的版本、source、sdist/wheel 哈希。
- `Makefile` — 统一调用 `.venv/bin/python -m pytest`、`ruff`、`uv build` 等命令。
- `.github/workflows/ci.yml` — CI 中执行 `make ci` 运行单元测试与集成测试。
- `AGENTS.md` / `DESIGN.md` — 文档中对依赖决策的记录（如 ADR 目录下的 OpenAI SDK、Pydantic v2 等）。

## 3. 架构与约定
- **依赖分层**：核心运行时依赖包括 openai>=2.0,<3.0、pydantic>=2.0,<3.0、anyio>=4.0,<5.0、httpx>=0.27,<1.0、pyyaml>=6.0,<7.0；可选依赖通过 [project.optional-dependencies] 按功能域拆分：dev（pytest、pytest-asyncio）、mcp、cron（apscheduler）、ssh（paramiko）、browser（playwright）。
- **版本约束策略**：所有依赖使用 >=X.Y,<Z.W 的半开区间，既允许小版本升级又防止破坏性大版本升级。
- **构建系统**：build-system.requires = ["hatchling"]，hatchling.build 作为后端，wheel 打包目标为 src/microagent。
- **可重复性**：uv.lock 锁定全部传递依赖及其哈希，确保任何环境安装结果一致；requires-python = ">=3.14" 明确最低 Python 版本。
- **无 vendoring**：不将第三方包内联到仓库，依赖均从 PyPI 拉取并通过 uv.lock 锁定。
- **私有源/代理**：当前未配置私有 registry 或 UV_INDEX_URL，全部来自 https://pypi.org/simple。

## 4. 开发者应遵循的规则
- **新增依赖**：在 pyproject.toml 的 dependencies 或对应 [project.optional-dependencies] 分组中添加，严格使用 >=X.Y,<Z.W 区间。
- **更新依赖**：使用 uv sync 或 uv lock 重新生成 uv.lock，提交锁文件变更，避免手动编辑。
- **可选依赖**：仅当模块真正需要时才放入 optional-dependencies，保持核心包最小化。
- **构建与发布**：通过 make build（即 uv build）生成 wheel/sdist，产物输出至 dist/。
- **测试环境**：使用 uv sync --extra dev 安装开发依赖，所有测试通过 .venv/bin/python -m pytest 执行。
- **禁止行为**：不要修改 uv.lock 中的哈希或版本号；不要在代码中硬编码第三方包路径；不要引入未声明的运行时依赖。