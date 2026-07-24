---
kind: frontend_style
name: 前端样式系统：本项目不涉及前端 UI 样式
category: frontend_style
scope:
    - '**'
---

经全面检索，该仓库为纯 Python 后端包（MicroAgent），不包含任何前端代码。仓库中没有 CSS、SCSS、LESS、SASS、Tailwind、HTML 模板或任何前端样式相关文件；grep 搜索 `css|scss|style|theme|tailwind|frontend|ui|html|template` 仅返回与构建产物目录（如 `build/`、`htmlcov/`）和文档中的无关匹配。项目通过 `pyproject.toml` + `Makefile` 组织 Python 包、CLI 入口、工具集与测试套件，属于无前端界面的服务端/库工程，因此 `frontend_style` 类别不适用于此仓库。