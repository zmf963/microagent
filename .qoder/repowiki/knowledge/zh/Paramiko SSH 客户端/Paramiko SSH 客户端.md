---
kind: external_dependency
name: Paramiko SSH 客户端
slug: paramiko
category: external_dependency
category_hints:
    - vendor_identity
scope:
    - '**'
---

Paramiko 提供 SSH 终端后端支持，允许通过 SSH 远程执行命令。与 LocalTerminal 和 DockerTerminal 并列作为终端后端之一。可选依赖 microagent[ssh]。