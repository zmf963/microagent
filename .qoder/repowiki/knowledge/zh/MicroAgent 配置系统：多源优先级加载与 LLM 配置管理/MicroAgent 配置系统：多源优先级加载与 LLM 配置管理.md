---
kind: configuration_system
name: MicroAgent 配置系统：多源优先级加载与 LLM 配置管理
category: configuration_system
scope:
    - '**'
source_files:
    - src/microagent/config.py
    - src/microagent/llm/client.py
    - src/microagent/surface/cli.py
    - pyproject.toml
---

## 1. 使用的系统与方案

MicroAgent 采用**轻量级多源配置解析器**，不依赖外部配置框架（如 pydantic-settings、dynaconf），而是通过自定义 `Config` dataclass + `from_file` 工厂方法实现。配置来源按明确优先级合并：**CLI 参数 > 环境变量 > YAML 配置文件 > 默认值**。

- **配置文件格式**：YAML（`pyyaml` 库）
- **存储位置**：用户家目录 `~/.microagent/config.yaml`
- **运行时配置对象**：`Config`（dataclass, frozen, slots=True）+ `LLMConfig`（OpenAI 兼容配置）
- **CLI 参数解析**：手写 argparse 风格的手动解析（无第三方库）

## 2. 核心文件与包

| 文件 | 作用 |
|------|------|
| `src/microagent/config.py` | 配置解析主逻辑，定义 `Config` 和 `from_file` 工厂 |
| `src/microagent/llm/client.py` | 定义 `LLMConfig` dataclass（base_url/api_key/model/reasoning_effort/service_tier） |
| `src/microagent/surface/cli.py` | CLI 入口，解析命令行参数并调用 `Config.from_file` |
| `pyproject.toml` | 声明 `pyyaml>=6.0,<7.0` 为必需依赖 |
| `tests/unit/test_config.py` | 配置模块的单元测试 |

## 3. 架构与设计决策

### 3.1 配置优先级链
```text
CLI args (--base-url, --api-key, --model, --system-prompt)
    ↓ (覆盖)
环境变量 (MICROAGENT_BASE_URL, MICROAGENT_API_KEY, MICROAGENT_MODEL, 
         MICROAGENT_SYSTEM_PROMPT, MICROAGENT_SKILLS_PATH)
    ↓ (覆盖)
配置文件 (~/.microagent/config.yaml)
    ↓ (覆盖)
硬编码默认值 (base_url="https://api.openai.com/v1", model="gpt-4o")
```

### 3.2 配置文件结构
```yaml
model:
  base_url: "https://api.openai.com/v1"
  api_key: "sk-..."
  model: "gpt-4o"
system_prompt: "You are a helpful assistant."
skills_path: "/path/to/skills:/another/path"
```

### 3.3 配置对象模型
- `Config`：顶层配置，包含 `llm: LLMConfig`、`system_prompt: str`、`skills_path: str | None`
- `LLMConfig`：OpenAI 兼容配置，支持 `reasoning_effort` 和 `service_tier` 等扩展字段
- 两者均为 `frozen dataclass + slots=True`，不可变且内存高效

### 3.4 错误处理策略
- 配置文件不存在 → 返回空字典，使用后续层级的默认值
- YAML 解析异常 → 静默忽略，回退到空配置
- API key 未设置 → CLI 启动时输出警告但不阻止运行

### 3.5 测试环境隔离
集成测试使用独立的 `MICROAGENT_TEST_*` 环境变量前缀，避免污染开发配置。

## 4. 开发者规范与约束

### 4.1 新增配置项的步骤
1. 在 `LLMConfig` 或 `Config` dataclass 中声明新字段
2. 在 `Config._read_config_file()` 中添加 YAML 字段映射
3. 在 `Config.from_file()` 中添加优先级合并逻辑（CLI > env > file > default）
4. 更新 CLI 参数解析（如需要）
5. 在 `__init__.py` 中导出新类型

### 4.2 环境变量命名约定
- 统一使用 `MICROAGENT_` 前缀
- 全大写，下划线分隔
- 示例：`MICROAGENT_BASE_URL`, `MICROAGENT_API_KEY`, `MICROAGENT_MODEL`

### 4.3 默认值设计原则
- 所有字段必须有合理的默认值，确保配置缺失时仍可运行
- 敏感信息（如 api_key）默认值为空字符串而非 None
- URL 默认指向 OpenAI 官方端点

### 4.4 向后兼容性
- 配置文件结构变更需保持向下兼容（未知字段被忽略）
- 新增可选字段不应破坏现有配置

### 4.5 安全注意事项
- API key 通过环境变量传递，避免硬编码
- 配置文件位于用户家目录，权限由操作系统控制
- 不支持 secrets manager 集成（当前设计如此）

## 5. 已知限制

- 不支持嵌套配置层级（仅一层 YAML）
- 不支持配置热重载
- 不支持配置验证（除 Pydantic 基础类型检查外）
- 不支持多个配置文件合并
- 不支持配置模板或变量替换
