# FlowRun 文档

FlowRun — Mermaid 流程图驱动的多 Agent 编排运行时（Python 3.11+ 纯标准库，零第三方依赖）。

## 快速入口

- [README](../README.md) — 项目主页：快速开始、功能一览、todo 指令标记
- [CLI 使用手册](usage.md) — 全部命令/参数/退出码/`flow.mmd` 语法要点

## 设计与实现

- [design.md](../design.md) — 设计总览：五域架构、图语法、事件文法、闸与恢复语义
- [interfaces.md](../interfaces.md) — 域间接口契约（冻结件）

## 工程

- [tasks.md](../tasks.md) — 任务清单与验收标准（M1 已收官）
- [CHANGELOG.md](../CHANGELOG.md) — 版本迭代记录
- [CONTRIBUTING.md](../CONTRIBUTING.md) — 贡献指南
- [examples/demo/README.md](../examples/demo/README.md) — 可运行样例说明

## 开发

```sh
# 全量测试（纯标准库，src 布局零安装可跑）
$env:PYTHONPATH="src"; python -m unittest discover -s tests

# 一键安装自验证（在克隆的仓库内、发布后运行）
pwsh scripts/verify-install.ps1
```