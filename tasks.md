# FlowRun 任务清单

> 配套 [design.md](./design.md)。三阶段推进：M1 可用的单线 → M2 控制面与人 → M3 规模与观感。
> 规则：每个任务带验收标准；勾选状态对应 ledger `node_done`（本文件本身也按"图是真相、todo 是投影"的精神维护——完成与否以验收标准为准）。
> 命名约定：所有模块/函数/文件名见名知意，禁止缩写歧义与数字后缀（如 `utils2.py`）。

---

## M1 · 可用的单线

**目标**：一张样例 flow.mmd，在 Claude Code 与 opencode 两个 agent 上跑通 顺序 + fork/join + 判定 + 人审，全程 ledger 可重放。

### 编译器（解释层）

- [x] **T001 定义 IR schema**：节点表/边表/谓词表的数据结构定稿（含 agent、out 契约、retry/timeout 边属性）。
  - 验收：样例图编译产物有 golden JSON 基准，可做回归对比。
  - 依赖：无（起点）。

- [x] **T002 flow.mmd 解析器**：frontmatter 剥离 + Mermaid flowchart 解析 + `;;` 节点配置提取 + 边标签（when/retry/timeout）解析；顺带定稿 `;;` 内 `]` 转义约定（倾向禁用+编译期报错）。
  - 验收：任务/判定/人审三类型节点 + retry/timeout/all 边全部解析正确；坏语法报出 Mermaid 行号。

- [x] **T003 IR 校验器**：图连通性、join 前驱需 `when: all` 声明、任务节点 `out` 契约非空、agent 引用存在于 frontmatter 注册表。
  - 验收：每一类坏图都报出行号与原因；好图零告警。

### 状态层

- [x] **T004 ledger 事件账本**：JSONL 追加写、全局 seq、prev-link 哈希链、`idem` 幂等键去重。
  - 验收：乱序/重复投递重放后状态一致；篡改任意一行能被链校验发现。

- [x] **T005 快照与恢复**：每 20 事件落快照；恢复 = 快照 + 重放增量。
  - 验收：kill -9 后续跑的最终 ledger 与不中断直跑语义等价。

### 适配层（文件契约面）

- [x] **T006 适配框架**：命令模板 + `__FLOW_*__` 占位符 + 每 CLI 一个声明式薄适配器 + `which` 探测（照抄 spec-kit 模式，拒绝三方言脚本镜像）。含**双信号退出监视器**（OS 退出码 vs 语义完成信号竞速，参考 vibe-kanban）。
  - 验收：新增一个假想 CLI 只需写一个声明文件即可跑通投递+回收。

- [x] **T007 Claude Code 适配器**：`-p --output-format stream-json` 投递、退出码与 result 消息双完成信号、产物回收。
  - 验收：packet 投递 → agent 执行 → 退出码 + `out=` 核对闭环。

- [x] **T008 opencode 适配器**：先做 `opencode run --format json` headless（M2 再评估切 ACP）。
  - 验收：同 T007。

### 执行内核

- [x] **T009 证据包判定器**：证据包组装（契约核对 + diff + agent 自报）→ 判定 LLM（温度 0，JSON Schema 强约束）→ 结构化 verdict；confidence < 0.6 重试 1 次。
  - 验收：缺任一 `out=` 产物必判 fail；verdict 不在出边集合必被拒。

- [x] **T010 内核状态机 + 三道闸**：节点生命周期（planned→ready→running→judging→done/failed/awaiting_human + paused/skipped 修饰态）；hop ≤ 4×节点数、token ≤ budget_tokens、非法边转移——三闸动作统一为 `gate_tripped` → 暂停转人审。
  - 验收：伪造非法 verdict 注入，内核转人审不崩溃；预算耗尽触发 `gate_tripped` 落账。

### 投影与冒烟

- [x] **T011 todo 投影**：从 ledger 派生勾选状态；用户三操作（人审行勾选 / `<!-- @pause -->` / `<!-- @skip -->`）落账生效；增删节点行忽略并告警。
  - 验收：`<!-- @pause -->` 后节点确实停在 ready；`<!-- @skip -->` 记事件不消失。
  - 注（2026-10-16 语法 v2）：指令只认**行尾 HTML 注释**形式，节点 label 里的裸 `@pause/@skip/@resume` 已失效。

- [x] **T012 AGENTS.stub.md 生成器**：内核为工作区生成的 agent 引导桩（说明 packet 协议、契约自报格式）。
  - 验收：新会话 agent 只读桩即能正确自报产物。

- [x] **T013 M1 端到端冒烟**：样例图（含 fork/join、条件判定、人审节点）双 agent 跑通。
  - 验收：全程 ledger 可全量重放且语义一致；失败注入（断网/缺产物）停在可审计位置。

---

## M2 · 控制面与人

**目标**：ACP 通道接入、审批闭环、retry/timeout 边生效、断点续跑端到端。

- [ ] **T014 acpx 复用评估 spike**：以子进程/库方式调用 acpx client，一段 ACP 会话的 turn 事件写入 ledger。
  - 验收：ACP 事件 → ledger 事件的映射表成文；确认不 fork。
  - 依赖：T004。

- [ ] **T015 ACP 适配器**：PromptResponse.stopReason → 完成语义；tool_call/usage_update 进观测；session/cancel 接入中断。
  - 验收：verdict 证据包可引用 ACP turn 的结构化事件（含 cost）。

- [ ] **T016 人审双通道**：`human_requested/human_resolved` 事件 + todo 勾选通道；ACP `request_permission` 映射同一 schema。
  - 验收：审批未决时图静止（无节点进入 running）。

- [ ] **T017 elicitation 表单映射**：ACP 结构化审批表单 → 审批结果结构化落账。
  - 验收：审批结果带选项与上下文，ledger 可重放。

- [ ] **T018 retry/timeout 边语义**：`retry: N` 耗尽转人审；`timeout: 30m` 到点走指定边（双信号竞速的优雅杀进程组 + 留 `agent_exited` 审计事件）。
  - 验收：超时分支被实际触发并落账；重试计数跨进程恢复正确。

- [ ] **T019 断点续跑端到端**：快照+重放 + 适配器会话续跑（Claude `--resume` / opencode `-s`）。
  - 验收：中断后续跑幂等（同一节点不会二次向 agent 投递 packet）。

- [ ] **T020 M2 端到端冒烟**：含人审 + 超时分支 + 断点续跑的完整剧本。
  - 验收：全剧本 ledger 可重放；三次随机中断点续跑结论一致。

### M1 遗留回流（收官核验发现，并入 M2 处理）

- [ ] **T020a 判定调用 usage 入账**：`judge_decision` 回传 usage 数字，纳入 `payload.tokens` 与 budget 闸（当前判定消耗只进 hop 闸不计 token）。
- [ ] **T020b 审计文件归位**：agent stdout/stderr 审计落 `audit/` 子目录而非 workspace 根；`agent_exited` 事件引用相对路径。
- [x] **T020c apply_user_marks 降噪**：done 派生的 `[x]` 行不再每轮重复告警。（2026-10-16 审计批次 C 线落地：非人审节点的派生勾选静默跳过，仅"未生效过"的标记告警并钉桩测试。）
- [ ] **T020d EventLedger 单实例化**：judge 收 ledger 参数改为收账本路径或共享单写者，消除 hooks_assembly 里"每次判读重建只读 ledger"的架构债。
- [ ] **T020e 失败处置语义定稿**：无 retry 边的失败节点当前触发 `gate_tripped(gate="retry", limit=0)`，与三闸语义混淆——定稿为独立的人审等待事件（`node_failed` → 明确的转人审标记），并区分 gate_tripped。
- [ ] **T020f FallbackAdapter 结构化提取**：支持从任意 agent 输出提取 usage（正则或标记约定），假 agent / 长尾 CLI 下 token_spent 不再恒 0。
- [ ] **T020g opencode 语义信号真机采样**：采集 `opencode run --format json` 真实输出样例，核定 idle/end 判定形态（当前为两类合理形态猜测实现）。
- [x] **T020h 文档位置修正**：`compile_flow_source` 实际位于 `flowrun.compiler.validator`（简报曾误标 mermaid_parser）。**核验结论**：全文检索 `*.md` 后，interfaces.md 目录树与各任务书均无该误标引用，无需改动即关闭。

---

### M1 审计修复批次（第一轮，2026-10-16，三线并行，已完成）

- [x] **A · 编译器/适配层 13 项**：命令注入 **P0**（Windows `.cmd/.bat` 垫片自动切换 path 注入模式，packet 正文一个字节不进 argv）、超时 kill 不达孙进程（`taskkill /T /F`）、`shlex` 吃 Windows 反斜杠（新增 `split_agent_command`）、`out=` 路径逃逸、frontmatter 引号残余/重复键/行内注释（严格化）、`when` 大小写、适配器匹配大小写、timeout 边组合约束、自环边、未闭合 subgraph、数值下限。结构拆分：新增 `mermaid_edge_label.py` / `mermaid_errors.py`。**新增 46 个测试。**
- [x] **B · 内核/状态层 5 项**：fan-in 错峰完成引发 done→ready 回退与重复投递（投影"只前进不后退"+ 边激活终态守卫双保险）、闸重复刷账与"等待点优先 + 每闸每 run 至多一次 + `flow_resumed` 永久豁免"、工作区身份校验（`FlowWorkspaceMismatchError`）、判定路径 probe 前置 + judge 异常兜底、judge 证据合并后总量上限。**新增 16 个测试。**
- [x] **C · CLI/todo 5 项**：`resume` 子命令与 todo `<!-- @resume -->` 通道（`flow_resumed` 生产者）、退出码语义分义（`3` 编译校验 / `4` 工作区不匹配 / `5` 未初始化）、`status/todo` 缺账本退 5、todo 头部 `FLOW PAUSED` 闸详情横幅、派生勾选静默化。**新增 13 个测试。**
- 验收证据：独立跑 `python -m unittest discover -s tests` → **294 tests OK**；每条修复均留"回退该修复即变红"留痕。文档漂移 D1-D13 已同步回写 design.md / interfaces.md。

### M1 审计修复批次（第二轮，2026-10-16，已完成）

- [x] **机器闸独立判定**：`hop` 与 `budget` 各自独立判定，同一轮可同时命中并各自落 `gate_tripped`；"每工作区每闸名至多一次 + 其后有 `flow_resumed` 即永久豁免"按**闸名**分别判定——一个闸被人审豁免不连坐另一个闸；收口前（⑦）复查与轮首（③）同口径。
- [x] **工作区身份收紧为严格集合相等**：`flow_started.planned_nodes` 与当前图节点集双向一致，多一个或少一个都拒（`kernel/engine.py::verify_workspace_matches_flow`）；`planned_nodes` 缺失/为空降级只校验 run 名，空账本不校验，非空但无 `flow_started` 不拦；校验零写入且在任何写入之前，`run`/`resume` 同一套；不符退 `4`。
- [x] **边缘激活终态守卫**：`edge_taken` 不改写终态（done/skipped/failed）target，内核 ⑤ 层遇到即跳过该边且不落 `edge_taken`；"整图收口"用 done/skipped，"边缘激活可否改写 target"用 done/skipped/failed；失败节点重投只经失败处置策略显式落的 `node_entered`。
- [x] **todo 指令标记语法 v2**：只认行尾 HTML 注释形式，裸标记失效并给每轮每文件至多一条升级告警；`@resume` 仍是全局标记（仅暂停态才落 `flow_resumed{by:"todo"}`）；头部/横幅注释不出现嵌套 HTML 注释。
- [x] **新增退出码 `6`（账本损坏）**：中段不可解析 / 哈希链断裂 / 快照核对失败 / 快照记录形状非法；CLI 入口统一捕获，stderr 给含 `ledger.jsonl` 路径与原因的可读诊断，不打 traceback。退出码表补全为 0=done、1=waiting、2=halted（M1 保留位）、3=编译/校验错、4=工作区身份不符、5=未初始化、6=账本损坏。
- [x] **`out=` 根相对路径拒绝**：新增拒绝 `\x`、`/x`、`\Windows\win.ini` 这类盘根相对形制，UNC 与盘符/绝对路径、`..` 段仍拒（`compiler/validator.py`）。
- [x] **`.ps1` 垫片纳入 path 模式**：`.cmd/.bat/.ps1` 一律 path 注入；`.cmd/.bat` 走 `subprocess.list2cmdline` 生成的整条命令行交 `create_subprocess_shell`，`.ps1` 走 `powershell -NoProfile -ExecutionPolicy Bypass -File`；win32 命令头按候选扩展集做有界 PATH 扫描，探测与派发同一入口；不可解析抛 `CommandHeadUnresolvableError`（继承 `FileNotFoundError`）。
- [x] **`split_agent_command`（win32）**：采用 `CommandLineToArgvW` 语义（优先 ctypes，不可用时等价手写回退）；引号内 `&`/`;`/`|`/`%` 字面量，空词丢弃，未闭合引号仍报错；非 win32 分支不变。
- [x] **恢复增量化**：链校验只校验新增条目（文件变短回退全量），最新快照记录进程内缓存 + 低成本失效判据；磁盘格式不变、恢复等价性不变。
- 验收证据：独立跑 `python -m unittest discover -s tests` → **358 tests OK**；本轮语义已回写 README.md / design.md / interfaces.md。

#### 本轮行为变更（面向用户，升级前请读）

- **工作区身份收紧**：由"只拦旧节点消失"改为**严格集合相等**——过去靠同 run 名复用工作区、且节点集不同的用法，升级后启动即拒（退出码 `4`）。
- **todo 标记语法 v2**：旧的裸 `@pause/@skip/@resume` 失效（给一条升级告警），须改为行尾 `<!-- @pause -->` 形式。
- **`.ps1` 垫片**：由 content 模式改为 path 模式。
- **新增退出码 `6`**（账本损坏）。

### M1 审计遗留（第二轮新发现，并入 M2 处理）

| 条目 | 现象 | 位置 |
|---|---|---|
| out= 放行 NTFS ADS | `a.txt:stream` 形式未被拦（非路径逃逸，但可指同目录数据流） | `compiler/validator.py` |
| cmd 垫片参数级注入面 | 模板参数本身含 `&`/`%`/`"` 时仍被 cmd.exe 重分词（暴露面与修复前同级，未新增） | `adapters/base.py` 启动计划 |
| shell 命令头 content 模式理论绕过 | `cmd`/`powershell` 命令头走 content 模式的理论绕过面（默认多行 packet 不可触发） | `adapters/base.py` 注入模式判定 |
| path 模式不中和 `%VAR%` | 仅当工作区路径含 `%…%` 时暴露 | `adapters/base.py` |
| 数值无上界 | `timeout` 可溢出为 `inf`；`budget_tokens`/`retry` 无上限 | `compiler/mermaid_edge_label.py`、`compiler/mermaid_parser.py` |
| `timeout:` 边可指向 `human` | 现只拒 join/decision | `compiler/validator.py` |
| 退出码 `2` 无生产者 | M1 无 `flow_halted`/`checkpoint` 写入方，`is_halted()` 恒 False、相关分支为死代码 | `cli.py`、`state/flow_state.py` |
| CLI 单一 `except OSError` 误报 | 写盘失败被报成"无法读取流程文件" | `cli.py` |
| 勾选未到达的人审节点即判 done | 勾选尚未到达 `awaiting_human` 的人审节点会被立即判 done | `kernel/todo_projection.py` |
| judge 异常后判节点进死胡同 | judge 异常兜底后判定节点无再审路径，resume 不可恢复 | `kernel/engine_node_executors.py`、`kernel/engine_gates.py` |
| usage 重复计数 | usage 同时含数字叶子与 `total_tokens` 时重复计数 | `kernel/engine_evidence.py` |
| 只读判定代理带写副作用 | 只读判定代理构造 `EventLedger` 会截断账本尾行 | `kernel/hooks_assembly.py`、`state/ledger.py` |
| 引擎账本句柄泄漏 | `run_flow` 抛异常时内部账本句柄不关闭（测试可见 `ResourceWarning: unclosed file ledger.jsonl`） | `kernel/engine.py` |
| 异常账本身份校验被跳过 | 账本非空但无 `flow_started` 时身份校验跳过、直接放行 | `kernel/engine.py` |
| 同源同目标多边被幂等合并 | 多条无条件边被幂等键合并，丢失"哪条 when 边走"的审计事实 | `kernel/engine_gates.py` |
| 恢复增量盲区 | 同进程内"原地改写已确认前缀"不可被增量路径察觉（跨实例首次仍全量、可检出） | `state/ledger.py` |
| `split_agent_command` 语义边界 | 空参数丢弃的语义将来若需传空参数会变；手写回退实现对病态引号序列与原生可能不一致 | `compiler/mermaid_parser.py` |
| 测试文件拆分 | 以下测试文件超 ~400 行组织约定，建议纯搬移拆分 | `tests/test_mermaid_parser.py`、`tests/test_compiler_validator.py`、`tests/test_cli.py`、`tests/test_todo_projection.py` |

> **存量台账复核（T020i–T020p）**：逐条复核后**均未关闭**——`T020i` 部分覆盖（见下）、`j/k/l/m/n` 原因未变维持开放、`o/p` 补注（见下）。

### M1 审计遗留（新发现，并入 M2 处理）

- [ ] **T020i 内核热路径 O(E²)**：`ledger.entries` 每次全量拷贝，长流程下判定/证据装配呈二次复杂度。
  - （2026-10-16 复核：本轮的**账本链增量校验 + 快照缓存**已覆盖**快照/恢复路径**的重复全量开销；判定/证据装配等其余热路径是否仍为 O(E²) 未实测，**保持开放**，不因部分覆盖而关闭。）
- [ ] **T020j 失败归因写进 payload**：`node_failed` 只写通用 reason，真实原因要从邻接事件反推。
- [ ] **T020k join 前驱不可达需提示**：join 存在永远无法到达的前驱时不报错、不设闸、不提示，流程静默悬挂。
- [ ] **T020l verdict.edge 口径统一**：判定协议给的是边编号（`D001->H001`），落账写的是目标节点 id——统一为边编号（design.md §5.2 已记）。
- [ ] **T020m 审计文件名去实例耦合**：审计文件名含进程/实例信息，跨 run 不稳定。
- [ ] **T020n 内核模块再拆分**：`hooks_assembly.py`（391 行）/ `engine_node_executors.py`（375 行）物理行数偏高，建议把"证据转换"段与 decision 执行器抽为独立模块（均低于 ~400 硬阈值，非阻塞）。
- [ ] **T020o 测试缺口补齐**：`@pause` 引擎级、闸后停住/恢复的引擎侧直测、`missing_outputs` 分支、T012 桩行为验收、"exit≠0 但产物齐"的语义完成组合，逐条钉桩。（`@pause` 用例按 v2 行尾 HTML 注释形式 `<!-- @pause -->` 钉桩。）
- [ ] **T020p 测试卫生**：全套偶发 `ResourceWarning: unclosed file … ledger.jsonl`（疑似某测试的只读 ledger 视图未关闭）。（2026-10-16 复核：已定位两条候选根因——`kernel/engine.py` 引擎异常时账本句柄泄漏、`kernel/hooks_assembly.py` 只读判定代理自建 `EventLedger`；两者已登记于本轮台账，待修复后复验。）

---

## M3 · 规模与观感

**目标**：并行、多 run、只读观测、生态扩展。

- [ ] **T021 并行调度**：`when: all` barrier 完整化，多任务节点并发投递、join 等齐。
  - 验收：双任务并发后 join 正确等齐；单侧失败走失败路径。
- [ ] **T022 多 run 隔离**：run_id 全链路贯穿（ledger/packet/todo/工作目录）。
  - 验收：双 run 并发互不串扰。
- [ ] **T023 只读观测 GUI**：读 ledger/快照渲染流程状态与成本（usage 汇总）。
  - 验收：严格只读，无任何写通道。
- [ ] **T024 交互图模板库**：`docs/` 下 sequenceDiagram 协议文档模板 + AGENTS.stub.md 引用生成。
  - 验收：编译器对 sequenceDiagram 文件明确忽略且不告警。
- [ ] **T025 Gemini CLI 适配器**：验证 headless 面的长尾（退出码 42/53 语义化利用）。
  - 验收：三家中最弱信号源下判定器仍可靠。
- [ ] **T026 subgraph 子流程语义评估**：是否升级为可执行单元（带结论的评估文档即可）。
- [ ] **T027 发布准备**：定名（查重名/商标）、README、许可协议（倾向 Apache-2.0）。

---

## 依赖速览

```
T001 → T002 → T003
T004 → T005 → T019
T006 → T007/T008 → T013
T009,T010 → T011 → T013
M1 全部 → M2（T014 依赖 T004 可提前） → M3
```

## 工程纪律（全程适用）

- 单文件超 ~400 行或多职责块必须拆分；入口只做装配。
- 行为零变化的结构调整与功能开发分 PR。
- 内核与判定器的所有对外输入按不可信处理（产物只进结构化抽取器）。
- 适配器零内部 API 依赖：只允许公开 CLI 面 / ACP / 文件系统。
