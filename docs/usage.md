# FlowRun CLI 使用手册

FlowRun 是一个 Mermaid 流程图驱动的多 Agent 编排运行时。本文档为 `flowrun` 命令的完整使用参考（命令行、参数、退出码、`flow.mmd` 语法要点）。

## 安装

纯标准库、Python ≥ 3.11、零第三方依赖。目前只从 GitHub 直装（PyPI 的 `flowrun` 是别人的同名包，与本项目无关；本项目发行名为 `flowrun-mmd`）：

```sh
pip install "git+ssh://git@github.com/dubuqiangu/flowrun.git"
```

## 命令总览

| 命令 | 作用 |
|---|---|
| `flowrun compile` | 编译校验 `flow.mmd` → 打印 FlowIR JSON |
| `flowrun run` | 驱动流程到等待点或完成 |
| `flowrun status` | 恢复并打印流程状态 |
| `flowrun todo` | 应用 todo 用户标记并重写 todo 投影 |
| `flowrun resume` | 解除闸/暂停挂起，落 `flow_resumed` 后可用 run 续跑 |
| `flowrun approve` | 人审节点裁决：通过 |
| `flowrun deny` | 人审节点裁决：否决 |

## 各命令详解

### `flowrun compile <flow_path>`

编译并校验一个 `flow.mmd`，把 Mermaid + frontmatter 编译为可执行的 FlowIR，打印为 JSON。坏图带行号报错。

```sh
flowrun compile flow.mmd
```

### `flowrun run <flow_path> --workspace <dir> [--dispatch-timeout <秒>]`

把流程驱动到等待点或完成。`--workspace` 指定工作区目录（必填）；`--dispatch-timeout` 指定任务投递超时秒数（默认见 `DEFAULT_DISPATCH_TIMEOUT_SECONDS`）。

```sh
flowrun run flow.mmd --workspace ws
flowrun run flow.mmd --workspace ws --dispatch-timeout 60
```

支持 fork/join、条件判定、人审、timeout/retry 边。结束时打印状态概览。

### `flowrun status --workspace <dir> [--flow <path>]`

恢复并打印流程状态（各节点状态、三闸计数、待人审节点、token/hop 计数）。`--flow` 提供后额外显示预算阈值。

```sh
flowrun status --workspace ws
flowrun status --workspace ws --flow flow.mmd
```

### `flowrun todo --workspace <dir> --flow <path>`

应用 `todo.md` 中的用户标记（行尾 HTML 注释 `<!-- @pause -->` / `<!-- @skip -->` / `<!-- @resume -->`）并重写 todo 投影。

```sh
flowrun todo --workspace ws --flow flow.mmd
```

### `flowrun resume --flow <path> --workspace <dir>`

解除闸/暂停挂起，落 `flow_resumed{by:"cli"}` 事件，之后可用 `run` 续跑。流若为 halted（终止）或 done（完成）或非暂停态会拒绝恢复。

```sh
flowrun resume --flow flow.mmd --workspace ws
```

### `flowrun approve <node_id> --flow <path> --workspace <dir>`

人审节点裁决：通过。落 `human_resolved{decision:"approve"}`，节点转 done。

```sh
flowrun approve H001 --flow flow.mmd --workspace ws
```

### `flowrun deny <node_id> --flow <path> --workspace <dir>`

人审节点裁决：否决。落 `human_resolved{decision:"deny"}`，节点转 failed。

```sh
flowrun deny H001 --flow flow.mmd --workspace ws
```

## 退出码

| 码 | 含义 |
|---|---|
| `0` | 完成（flow done） |
| `1` | 等待（人审或暂停） |
| `2` | 终止（halted，M1 保留位，无生产者） |
| `3` | 编译或校验错误 |
| `4` | 工作区身份不符（多/少节点，换图请换工作区） |
| `5` | 工作区未初始化（缺 `ledger.jsonl`） |
| `6` | 账本损坏（中段不可解析 / 哈希链断裂 / 快照核对失败 / 快照记录形状非法） |

退出码 `6` 由 CLI 入口统一捕获，stderr 输出含 `ledger.jsonl` 路径与原因的可读诊断，不打 Python traceback。

## `flow.mmd` 语法要点

一个 `flow.mmd` 由 frontmatter + Mermaid 图组成：

```md
---
run: quick-start
defaults: {agent: my-agent, budget_tokens: 200000}
agents:
  my-agent: {cmd: "claude -p --output-format stream-json"}
---
flowchart LR
    T001[实现登录API;;agent=my-agent;;out=src/auth.py] --> D001{验收通过?}
    D001 -->|when: ok| H001([发布人审])
    D001 -->|when: fail| T003[失败诊断]
```

- **frontmatter**：`run`（run 名）、`defaults`（默认 agent / 预算）、`agents`（agent 声明，`cmd` 指定 CLI 命令）。
- **节点**：任务节点 `[...]`、判定节点 `{...}`、人审节点 `([...])`、join `((...))`。
- **边**：`-->` 顺序、`-->|when: ok/fail|` 条件分叉、`timeout: 30m` 超时走边、`retry: N` 重试。
- **完整样例**见 [examples/flow.mmd](../examples/flow.mmd) 与可运行样例 [examples/demo/README.md](../examples/demo/README.md)。

## 相关文档

- [docs/index.md](index.md) — 文档索引
- [../README.md](../README.md) — 项目主页
- [../design.md](../design.md) — 设计总览（五域架构、图语法、事件文法、闸与恢复语义）
- [../interfaces.md](../interfaces.md) — 域间接口契约（冻结件）
- [../tasks.md](../tasks.md) — 任务清单与验收标准