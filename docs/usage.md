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

## `flow.mmd` 语法参考

一个 `flow.mmd` 由 **frontmatter**（YAML，流级默认值与 agent 注册表）+ **Mermaid 图**（拓扑）组成。编译器先剥除 frontmatter，再解析图。

### frontmatter

```yaml
---
run: release-login                    # run 名（必填）
defaults: {agent: claude-code, budget_tokens: 1000000}   # 默认 agent（必填）+ 预算（可选，默认 1M）
agents:                                # agent 注册表
  claude-code: {cmd: "claude -p --output-format stream-json", resume: session}
  opencode:   {cmd: "opencode run --format json --file __FLOW_PACKET_PATH__"}
---
```

| 键 | 必填 | 说明 |
|---|---|---|
| `run` | ✅ | run 名，决定工作区子目录与身份 |
| `defaults.agent` | ✅ | 全局默认 agent，节点未指定 `agent=` 时用 |
| `defaults.budget_tokens` | ❌ | token 预算，默认 1,000,000；超限触发 budget 闸 |
| `agents` | ❌ | agent 注册表；`cmd` 指定 CLI 命令，`resume` 指定会话模式 |

### 节点

| 记号 | 类型 | 说明 |
|---|---|---|
| `T001[标签]` | 任务 | 由 agent 执行；`;;` 分隔的键值对为节点内配置 |
| `D001{标签}` | 判定 | 出边按判定 LLM 评估结果分叉 |
| `H001([标签])` | 人审 | 强制人审点，需 approve/deny 或勾选 todo |
| `J001[标签]` | join | 收口屏障，`-->|when: all|` 等前驱全部完成才推进 |

**节点内配置**（`;;` 键值对，仍是合法 Mermaid 文本）：
- `agent=<name>`：哪个 agent 执行（默认取 frontmatter `defaults.agent`）
- `out=<文件,清单>`：**产物契约**，逗号分隔相对路径，判定器逐项核对存在性，缺项即 fail。路径必须相对、不含 `..`、非绝对路径/盘根/UNC，违者编译报错。

### 边

| 记号 | 语义 |
|---|---|
| `-->` | 顺序边 |
| `-->|when: all|` | join 收口条件（前驱全部完成后推进） |
| `-->|when: <自然语言谓词>|` | 条件边——由判定 LLM 评估。M1 限制：谓词文本不能含逗号 |
| `-->|when: fail, retry: 2|` | 回边：失败重试 2 次，耗尽转人审 |
| `-->|timeout: 30m|` | 超时分支——`when=None` 的无条件边 + 定时器门控，定时器先到即走此边 |

### 完整样例

```md
---
run: release-login
defaults: {agent: claude-code, budget_tokens: 1000000}
agents:
  claude-code: {cmd: "claude -p --output-format stream-json", resume: session}
  opencode: {cmd: "opencode run --format json --file __FLOW_PACKET_PATH__"}
---
flowchart TD
  T001[实现登录API;;agent=claude-code;;out=src/auth.py,test/auth.spec.ts] --> J001
  T002[实现登录UI;;agent=opencode;;out=src/ui/Login.tsx] --> J001
  J001[集成准备] -->|when: all| D001{验收通过?}
  D001 -->|when: ok| H001([发布人审])
  H001 --> T004[打 tag 发布]
  D001 -->|when: fail| T003[失败诊断;;agent=claude-code]
  D001 -->|timeout: 30m| T005[超时诊断;;agent=claude-code]
```

完整样例见 [examples/flow.mmd](../examples/flow.mmd)；可运行样例见 [examples/demo/README.md](../examples/demo/README.md)。

## 端到端工作流

以下用一个 `flow.mmd` 走完整生命周期（编译 → 执行 → 查看 → 人审 → 恢复 → todo）：

```sh
# 1. 编译校验，确认图合法并查看 FlowIR
flowrun compile flow.mmd

# 2. 驱动执行到等待点或完成
flowrun run flow.mmd --workspace ws

# 3. 查看当前状态（节点状态 / 闸 / 待人审 / token/hop）
flowrun status --workspace ws --flow flow.mmd

# 4. 流停在人审点 H001 —— 裁决：通过
flowrun approve H001 --flow flow.mmd --workspace ws
#    或否决
flowrun deny H001 --flow flow.mmd --workspace ws

# 5. 若因闸（hop/budget）或 `@pause` 暂停 —— 解除挂起
flowrun resume --flow flow.mmd --workspace ws

# 6. 恢复后重新驱动
flowrun run flow.mmd --workspace ws

# 7. 随时用 todo 投影管理人审清单（勾选回写）
flowrun todo --workspace ws --flow flow.mmd
```

## 工作区布局

工作区根**无** `ledger.jsonl` → 每个 run 独占 `<workspace>/<run>/`（多 run 隔离）；根**有** `ledger.jsonl` → legacy 扁平布局。

`run` 目录 / legacy 根下会生成：

- `ledger.jsonl` — 事件账本（追加写，sha256 前链绑定）
- `snapshots/` — 恢复快照
- `packets/` — 任务包（给 agent 喂的 prompt）
- `audit/` — agent stdout/stderr 审计（`<node_id>-<kind>-<attempt>.stdout.log`）
- `todo.md` — 人审清单投影
- `AGENTS.stub.md` — agent 提示模板（工作区级，留根共享）

## todo 指令标记

指令只在**节点行行尾的 HTML 注释**里生效：

```md
- [ ] T002 生成报告 <!-- @pause -->
- [ ] T005 清理缓存 <!-- @skip -->
- [ ] T007 发布 <!-- @resume -->
```

- `@pause` 挂起此处；`@skip` 跳过该节点（行保留但不勾选）；`@resume` 解除暂停。
- `@resume` 是**全局**标记：仅当流确为暂停态才落 `flow_resumed{by:"todo"}`，否则告警忽略。
- 节点 label 文本里的裸 `@pause`/`@skip`/`@resume` **永不触发**——label 是数据，不是指令。
- 生成的头/横幅注释**不得出现嵌套 HTML 注释**（Markdown 会提前闭合）。

## 相关文档

- [docs/index.md](index.md) — 文档索引
- [../README.md](../README.md) — 项目主页
- [../design.md](../design.md) — 设计总览（五域架构、图语法、事件文法、闸与恢复语义）
- [../interfaces.md](../interfaces.md) — 域间接口契约（冻结件）
- [../tasks.md](../tasks.md) — 任务清单与验收标准