# FlowRun 样例：一条完整的小发布流程

这是一个**可以直接跑**的样例，用来看清 FlowRun 在真实运行中怎么监控与推进一条流程。

它演示的不是"模型能不能写出好代码"，而是**流程控制**：并行派发、产物核对、判定分叉、
人审挂起、闸触发与恢复。所以这里的两个 agent 是**确定性假 agent**——不联网、不调用任何
LLM，跑两次结果一模一样，方便你对着账本逐条核对。

## 跑起来

在仓库根目录（`flowrun/`）：

```powershell
python examples/demo/run_demo.py           # 完整剧本，跑完自动清理临时目录
python examples/demo/run_demo.py --keep    # 保留临时目录，方便自己翻看工作区
```

输出末尾会有一张验收表，`20/20 项通过` 且退出码 0 才算跑通。

## 流程长什么样

`flow.mmd` 是真相源（Mermaid flowchart）：

```
T001 写登录 API ─┐
                 ├→ J001 集成准备 → D001 验收通过? ─when: ok→ H001 发布人审 → T004 打 tag 发布
T002 写登录 UI  ─┘                        │
                                          ├─when: fail──→ T003 失败诊断
                                          └─timeout: 30m→ T005 超时诊断
```

node 用 `;;` 挂属性：`agent=` 指定用哪个 agent，`out=` 声明**该节点必须交付的产物**。
`out=` 是产物核对的依据——agent 自称做完不算数，文件不在就是没做完。

## 剧本的七步，以及每步该看到什么

| 步骤 | 做什么 | 期望 |
|---|---|---|
| [0] | `compile demo.mmd` | 退出码 0，打印 FlowIR JSON（节点、边、entry 节点） |
| [1] | `run` | 退出码 **1**，`flow: in-progress`，H001 为 `awaiting_human`，T001/T002/J001/D001 都是 `done` |
| [2] | `status` / `todo` | `status` 只读打印同一份状态；`todo` 重写 `todo.md`，H001 那行带 `<!-- awaiting_human -->` |
| [3] | `approve H001` → 再 `run` | 退出码 **0**，`flow: done`，T004 产出 `release-tag.txt` |
| [4] | 失败路径 A：agent 自称做完却没写产物 | 退出码 **1**，`reason=missing_outputs` + `已触发闸: retry` |
| [5] | 失败路径 B：判定回不存在的边 | 退出码 **1**，`verdict` 里 `edge=null, tripped=true`，`已触发闸: verdict` |
| [6] | 失败路径 C：agent 进程级失败 | 退出码 **1**，`reason=nonzero_exit`，`已触发闸: retry` |

## 四个监控面（这就是"怎么监控执行"）

跑完 `--keep`，工作区里有四样东西可看：

| 面 | 位置 | 看什么 |
|---|---|---|
| 账本 | `workspace/ledger.jsonl` | 一切的事实来源。追加写、`prev_hash` 哈希链，逐条可校验 |
| 状态 | `flowrun status --workspace <ws> --flow <mmd>` | 从账本 + 快照恢复出的当前节点状态、token/hop 用量与三闸阈值 |
| 待办 | `workspace/todo.md` | 执行态的**投影**（不是真相源）：勾选某行即对节点下指令，可用 HTML 注释标 `@pause` / `@skip` / `@resume` |
| 审计 | `workspace/audit/<节点>-<种类>-<attempt>.stdout.log` | 每次派发 agent / 判定 agent 的原始 stdout，配 `.stderr.log` |

账本用 `seq` + `type` + `node` 就能读，例如成功那次：

```
flow_started
agent_dispatched[T001] → agent_exited[T001] → node_done[T001]
agent_dispatched[T002] → agent_exited[T002] → node_done[T002]
edge_taken[T001] → edge_taken[T002] → node_done[J001] → edge_taken[J001]
agent_dispatched[D001] → agent_exited[D001] → verdict[D001] → edge_taken[D001]
human_requested[H001] → human_resolved[H001] → edge_taken[H001]
agent_dispatched[T004] → agent_exited[T004] → node_done[T004]
flow_done
```

注意 `agent_exited` 的 payload 里 `semantic_done: false`——假 agent 只打印自报文本和一行
usage JSON，并没有宣称自己"语义完成"。**节点是否完成由产物核对决定**，不由 agent 自述决定：
失败路径 A（`--mode lie`）就是退出码 0、自报"已完成"，但一个文件都没写，被核对拦下并记
`reason=missing_outputs`。这是最需要防的一类失败——agent 的叙事和交付不一致。

`verdict` 那条约 payload：

```json
{"confidence": 0.95, "edge": 3, "evidence": ["上游产物齐全"], "tripped": false}
```

`edge` 是**该边在 `flow_ir.edges` 中的声明序号**（0 起，与 `compile` 打印的 `edges` 数组下标
一致；`D001->H001` 是第 3 条，故为 `3`），不是候选清单里的 1-based 编号，也不是目标节点名。
判定 agent 回的边若不在候选集合里，就会被判 `edge: null, tripped: true`，原始回包也记在
`verdict` 的 `evidence` 里方便对照。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 完成（`flow_done`） |
| 1 | 等待（暂停 / 待人审） |
| 2 | halted |
| 3 | 编译·校验·IO 失败 |
| 4 | 工作区与流程身份不符 |
| 5 | 工作区未初始化 |
| 6 | 账本损坏（中段不可解析或哈希链断裂） |

## 文件

| 文件 | 作用 |
|---|---|
| `flow.mmd` | 流程模板。两条 `cmd` 是占位符 `__DEMO_WORKER_CMD__` / `__DEMO_JUDGE_CMD__` |
| `demo_agent.py` | 假任务 agent：按 packet 的 `contract: [a, b]` 创建产物 + 打印 usage；`--mode lie` 自称做完但不写文件，`--mode crash` 异常退出 |
| `demo_judge.py` | 假判定 agent：`--mode hit` 回候选清单第一条边，`--mode miss` 回一个不存在的边 |
| `run_demo.py` | 驱动脚本：填占位符 → 跑七步 → 自动断言 → 打印验收表 |

### 为什么 `cmd` 要由脚本填

适配器是**以 workspace 为 cwd** 拉起 agent 子进程的，而 `cmd:` 只支持
`__FLOW_PACKET__` / `__FLOW_PACKET_PATH__` 两个占位符，没有"仓库根"。所以样例命令必须是
绝对路径，由 `run_demo.py` 在生成 `flow.mmd` 时填好——这样仓库里不会固死某台机器的路径。

如果你已经 `pip install -e .` 或把 `examples/demo` 里的脚本装成了 PATH 上的命令，也可以把 `cmd`
写成 `flowrun-demo-agent` 这类短命令（`cmd:` 就是一条 argv，FlowRun 不关心它怎么解析）。
本仓库没有为你预置这样的入口——样例走的是绝对路径，因为那是唯一在"任意机器的任意
cwd"下都成立的形式。

## 换成真 agent

把 `flow.mmd` 里的 `agents:` 换成你已有的 headless CLI 即可，例如：

```yaml
agents:
  claude-code: {cmd: "claude -p --output-format stream-json", resume: session}
  opencode:    {cmd: "opencode run --format json --file __FLOW_PACKET_PATH__"}
```

只要你的 agent 能读到 packet（作为最后一个位置参数传入，或用 `__FLOW_PACKET_PATH__` 指路），
并把 `out=` 声明的产物写到 workspace 里，就不需要改它一行代码。判定节点则需要在 stdout
打印一行严格 JSON verdict（字段见 `design.md` §5.2）。