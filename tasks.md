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
- [x] **新增退出码 `6`（账本损坏）**：中段不可解析 / 哈希链断裂 / 快照核对失败 / 快照记录形状非法；CLI 入口统一捕获，stderr 给含 `ledger.jsonl` 路径与原因的可读诊断，不打 traceback。退出码表补全为 0=done、1=waiting、2=halted、3=编译/校验错、4=工作区身份不符、5=未初始化、6=账本损坏（`2` 的**保留语义于第七轮定稿为"测试钉住的契约"**：账本一旦含 `flow_halted`，`run`/`resume`/`status` 一律报 `2`；仍**无生产者**、也**不新增 `halt` 子命令**，生产者随 T019 落地）。
- [x] **`out=` 根相对路径拒绝**：新增拒绝 `\x`、`/x`、`\Windows\win.ini` 这类盘根相对形制，UNC 与盘符/绝对路径、`..` 段仍拒（`compiler/validator.py`）。
- [x] **`.ps1` 垫片纳入 path 模式**：`.cmd/.bat/.ps1` 一律 path 注入；`.cmd/.bat` 走整条命令行交 `create_subprocess_shell`（**注意**：命令行渲染自第五轮起已由 `subprocess.list2cmdline` 换成 `adapters/cmd_line_safety.py::render_cmd_exe_arguments`，见第四/五轮批次），`.ps1` 走 `powershell -NoProfile -ExecutionPolicy Bypass -File`；win32 命令头按候选扩展集做有界 PATH 扫描，探测与派发同一入口；不可解析抛 `CommandHeadUnresolvableError`（继承 `FileNotFoundError`）。
- [x] **`split_agent_command`（win32）**：采用 `CommandLineToArgvW` 语义（优先 ctypes，不可用时手写回退）；引号内 `&`/`;`/`|`/`%` 字面量，空词丢弃，未闭合引号仍报错；非 win32 分支不变。**第七轮把语义边界钉成 fail-loud**（原"不可用时**等价**手写回退"的说法是错的）：回退实现对"引号段落内出现 `""`"这一病态形式**抛 `ValueError`**，且 win32 上 `split_agent_command` **对拍原生与回退、不一致即抛**。右下限如实记录：stdlib 随机语料 28687 条，静默分歧 **3567 → 2730**（回退侧守卫**不足以**达标，win32 的"零静默分歧"来自对拍）；**无 ctypes 的退化环境下仍有约 9.5% 病态输入会被静默错误切分**（另开工单：忠实复现 `CommandLineToArgvW`，或原生不可用时直接拒绝切分）。实现已抽到 `compiler/agent_command_splitter.py`（`mermaid_parser` 再导出**同一对象**，`__all__` 逐字未改）。
- [x] **恢复增量化**：链校验只校验新增条目（文件变短回退全量），最新快照记录进程内缓存 + 低成本失效判据；磁盘格式不变、恢复等价性不变。**已知边界（第七轮公开为契约）**：同实例内对**已确认前缀**做等长原地改写不被增量校验察觉——这是**刻意的**（每轮全量重扫会把 O(N) 恢复劣化成 O(N²) I/O）；覆盖手段是 `close → reopen`（新实例一律全量重扫、能检出），边界由 `tests/test_ledger_incremental_verify_boundary.py` 钉住。实测：全量 500 条 4.99ms → 4000 条 38.2ms（线性），空操作增量恒 ≈0.19ms（倍率 24× → 197×）。
- 验收证据：独立跑 `python -m unittest discover -s tests` → **358 tests OK**；本轮语义已回写 README.md / design.md / interfaces.md。

### M1 审计修复批次（第三轮，2026-10-18，已完成）

- [x] **usage 重复计数**：`extract_usage_tokens` 对 dict 形态 usage 不再无条件求和——先取 `total_tokens`（缺失再取 `total`）作为总量；两者皆缺时排除总数语义键后只把非布尔数字叶子相加，消除"叶子 + 总数"双重计入（`kernel/engine_evidence.py`）。
- [x] **只读判定代理带写副作用**：新增 `EventLedger.open_readonly()`——共用初始化但**不截断尾行、不惰性开写句柄、`append()` 抛 `RuntimeError`**；`hooks_assembly` 的判定旁路读取改用该视图，判定对账本零写副作用（`state/ledger.py`、`kernel/hooks_assembly.py`）。
- [x] **引擎账本句柄泄漏**：`run_flow` 主体包 `try/finally`，异常路径（含身份校验抛错）也关闭账本句柄（`kernel/engine.py`）。
- [x] **测试辅助句柄卫生（T020p 根因）**：5 个引擎测试类的 `reopened_ledger()` 统一 `addCleanup(ledger.close)`；临时目录清理由 `tearDown` 改为 `setUp` 内 `addCleanup`，保证 LIFO 下"先关账本、后删目录"（否则 Windows 上仍打开的 `ledger.jsonl` 会让 `rmtree` 失败）。
- 新增测试：`tests/test_ledger_readonly_view.py`（7）+ `tests/test_kernel_engine_ledger_lifecycle.py`（3）+ `tests/test_kernel_usage_accounting.py`（10）＝ **+20**。
- 验收证据：独立跑 `python -m unittest discover -s tests` → **386 tests OK**；另以 `python -W always::ResourceWarning -m unittest discover -s tests` 复跑，`ResourceWarning: unclosed file` **归零**（"引擎异常路径已释放句柄"以 `os.replace` 成功为主证据，比 ResourceWarning 稳）。

#### 本轮行为变更（面向用户，升级前请读）

- **工作区身份收紧**：由"只拦旧节点消失"改为**严格集合相等**——过去靠同 run 名复用工作区、且节点集不同的用法，升级后启动即拒（退出码 `4`）。
- **todo 标记语法 v2**：旧的裸 `@pause/@skip/@resume` 失效（给一条升级告警），须改为行尾 `<!-- @pause -->` 形式。
- **`.ps1` 垫片**：由 content 模式改为 path 模式。
- **新增退出码 `6`**（账本损坏）。

### M1 审计修复批次（第四轮，2026-10-18，已完成）

**编译器/校验层（Lane A）**
- [x] **`out=` 放行 NTFS ADS**：`out=` 值中出现 `:` 一律编译期拒绝——`file.txt:stream` 既有空 drive、又非绝对路径，故此前躲过盘符/绝对路径判定，却指向同一目录项上的隐藏数据流。该检查**刻意排在盘符检查之后**，`C:\x` 等仍保留原 "must be a relative path" 文案，零回归（`compiler/validator.py`）。
- [x] **`timeout:` 边可指向 `human`**：拒绝集由 `(join, decision)` 扩为 `(join, decision, human)`——否则超时分支会绕过人审（`compiler/validator.py`）。
- [x] **数值无上界**：新增 `compiler/limits.py` 集中三个上限并强制生效——`MAX_TIMEOUT_SECONDS = 30 天`（先 `math.isfinite` 拦住"超长数字溢出成 `inf`"）、`MAX_BUDGET_TOKENS = 1e9`、`MAX_RETRY_ATTEMPTS = 99`；**等于上限接受，严格超上界才拒**（`compiler/mermaid_edge_label.py`、`compiler/mermaid_parser.py`）。

**控制面（Lane B）**
- [x] **CLI 单一 `except OSError` 误报**：读流程文件失败收敛为专用 `FlowFileReadError`（直接继承 `Exception`，非 `OSError` 子类，故与 `except OSError` 无顺序依赖）；`main` 拆两子句——读失败报"无法读取流程文件"，其余 `OSError` 报"工作区写盘失败（账本 / todo / 产物等无法写入）"。退出码仍为 `3`（既有语义表"编译·校验·IO 失败"），**未新增码**（`cli.py`）。
- [x] **勾选未到达的人审节点即判 done**：`_append_human_resolution` 增加状态门槛——**仅 `awaiting_human` 才落 `human_resolved`**；未到达即勾选不落账、不判 done，并按降噪政策每轮每文件至多一条同类告警（新增 `_warn_category_once`）（`kernel/todo_projection.py`）。
- 测试前置修正：`tests/test_todo_projection.py` 的 `_prime_ledger_and_write_todo` 原先只落 `flow_started`（H001 实为 `ready`），其断言依赖了上述缺陷行为；现补一条 `human_requested` 使之真处于 `awaiting_human`。**这是补正确前置条件，不是放宽断言**——该文件其余用例（pause/skip/resume/幂等）语义不变，未到达即勾选的拦截另由新测试文件覆盖。
- 新增测试：`tests/test_compiler_limits.py`（7）+ `tests/test_compiler_out_and_edge_rules.py`（6）+ `tests/test_cli_io_errors.py`（3）+ `tests/test_todo_projection_human_guard.py`（4）＝ **+20**。
- 验收证据：独立跑 `python -m unittest discover -s tests` → **406 tests OK**；`-W always::ResourceWarning` 复跑 406 OK 且 `ResourceWarning` **归零**。两条修复线各自做了"回退即变红"验证：编译侧 7 条拒绝类用例全红、控制面 3 条红（写盘误报 / 未到达即勾选 ×2）。
- 集成期发现的两处工具链噪声（已修，非产品缺陷）：Lane A 把三个既有源文件整体翻成 CRLF（索引仍是 LF），已归一为 LF，真实改动收敛为 +50/−6；PowerShell 的 `Measure-Object -Line` **跳过空行**，导致此前全仓行数低估约 15%（口径修正见下）。

#### 本轮行为变更（面向用户，升级前请读）

- **`out=` 含 `:` 现在编译报错**：过去 `out=a.txt:stream`（NTFS 备用数据流）能通过。
- **`timeout:` 边不再允许指向人审节点**：过去可用，升级后编译报错（超时分支会绕过人审）。
- **数值上限生效**：`timeout` > 30 天、`budget_tokens` > 10^9、`retry` > 99 一律编译报错。
- **勾选未到达的人审节点不再判 done**：过去会被立即判 done（等于伪造人审通过），现在忽略并给一条告警。
- **CLI IO 失败文案分诊**：读流程文件失败与工作区写盘失败现在给不同诊断文案（退出码都仍是 `3`）。

### M1 审计修复批次（第五轮，2026-10-18，已完成）

**适配层注入面（Lane C）**
- [x] **cmd 垫片参数级注入面**：把 `subprocess.list2cmdline` 换成集中渲染器 `render_cmd_exe_arguments`（新增 `adapters/cmd_line_safety.py`）——**每个参数无条件加双引号**，`&` / `|` / `^` / `<` / `>` / 空白 一律失活。**刻意不采用"仅含元字符时才加引号"**：安全关键渲染不应依赖"元字符枚举是否完整"，漏一个字符注入面就回来了。
- [x] **path 模式不中和 `%VAR%`**：`%` 在 cmd.exe 里**即使位于双引号内也会展开**，`%%` 也非命令行层面的可靠转义 → 只能**失败关闭**：参数含 `%` / `"` / CR / LF 时拒绝执行并抛 `CmdExeUnsafeArgumentError`（文案给出"把工作区移到不含 `%` 的路径"的可执行建议）。**边界**：直接经 `CreateProcess` 启动的进程（`.exe`、`powershell -File`）不做此限制——那里 `%` 是字面量，不误伤。
- [x] **shell 命令头 content 模式理论绕过**：命令头是已知 shell（`cmd` / `cmd.exe` / `powershell(.exe)` / `pwsh(.exe)` / `sh(.exe)` / `bash(.exe)`，按**文件名**判定、忽略目录与大小写）+ content 注入 → **编译期拒绝**（`ShellCommandHeadContentInjectionError`）。**不自动改 path 模式**：把 packet 路径喂给一个 shell 会试图执行它，比拒绝更危险。前端**没有开关**可配（注入模式是从命令头**派生**的）。
  - 判定在 `adapters/registry.py`（`require_safe_injection_for_declared_agent` / 整表入口 `validate_agent_injection_policy`），报告在 `compiler/validator.py`（逐 agent 收集为 `ValidationError`，退出码 `3`）——使"拿到 FlowIR ⇒ 注入策略已校验"成为真不变量，`compile`/`run`/`resume` 全都受益，且拒绝发生在**任何写入之前**。
  - **性能口径**：`resolve_injection_mode` 含一次有界 PATH 扫描（**实测 51ms/次**未命中、26ms/次命中），故先做**纯名字预筛**（`is_known_shell_command_head`，零探测），只有确为 shell 命令头才解析注入模式——正常 agent（claude / opencode）零代价。实测加固后编译耗时 **0.071ms/次**，与加固前 0.073ms/次持平。

**内核（Lane D）**
- [x] **异常账本身份校验被跳过**：`verify_workspace_matches_flow` 由 fail-open 改 **fail-closed**——账本非空却缺 `flow_started`、或 `flow_started` 的 `planned_nodes` 缺失/为空，均拒绝（退 `4` + "请换新工作区"），仍在一切写入之前、零写入；空账本（`last_seq == 0`）仍放行（`kernel/engine.py`）。
- [x] **同源同目标多边被幂等合并**：`edge_taken` 幂等键由 `source->target` 升级为**单条边唯一**：`<run>-edge-<index>-<source>-><target>-<kind>`（`index` = 边在 `flow_ir.edges` 的声明序号，`kind ∈ {when, retry, timeout, auto}`），`payload` 增 `kind`；配套 `_edge_position_in_flow` 用对象同一性定位、找不到即 `ValueError`（fail-loud）（`kernel/engine_gates.py`）。
- [x] **judge 异常后判节点进死胡同**：判定节点执行 judge 抛异常 → 边界捕获、**异常绝不穿透**，落**既有**终态 `node_failed`（`payload["reason"] = "<异常类型>: <消息>"`，上限 500 字符后加 `...(已截断)`），**未新增任何事件类型**；下游暂停由既有 `retry` 闸承接（**不** trip `verdict` 闸，避免同一次失败双重闸）（`kernel/engine_node_executors.py`）。
- 新增测试：`test_adapter_cmd_line_safety.py`(14) + `test_adapter_shell_head_policy.py`(9) + `test_kernel_workspace_identity_strict.py`(10) + `test_kernel_edge_identity.py`(4) + `test_kernel_judge_failure_terminal.py`(6) + `test_validator_injection_policy.py`(6，接线) ＝ **+49 条**。
- 被修改的既有测试（**均为纠正被缺陷钉住的前置条件，不是放宽断言**）：`test_kernel_engine_workspace_identity.py`（缺 `planned_nodes` 由"只比 run 名"改判"拒绝"）、`test_cli.py`（同类，改判退 `4`）、`test_kernel_engine_judge_fallback.py`（judge 异常由"trip verdict 闸"改判 `failed` + `reason` + 既有 retry 闸）、`test_windows_shim.py`（**仅 1 行格式断言**由裸 token 改带引号形态，`BatchShimInjectionSafetyTest` 等安全断言一字未动）。
- 验收证据：独立跑 `python -m unittest discover -s tests` → **455 tests OK**；`-W always::ResourceWarning` 复跑 455 OK 且 `ResourceWarning` **归零**；全仓 `git ls-files --eol` 无 `w/crlf`；垃圾碎片扫描 0 命中。两条修复线各自做了"回退即变红"验证（适配侧 3 类红、内核侧缺陷 1 五处红 / 缺陷 2 一条红 / 缺陷 3 五处红）。

#### 本轮行为变更（面向用户，升级前请读）

- **旧账本跨版本续跑不兼容**：`edge_taken` 幂等键格式已变更（`<run>-edge-<source>-<target>` → `<run>-edge-<index>-<source>-><target>-<kind>`），同工作区升级内核后**已落账的边可能被重新激活** → **升级请换新工作区**。
- **"无法校验身份"不再等于放行**：过去账本非空但缺 `flow_started`、或 `flow_started` 缺 `planned_nodes` 会被放行，现在退 `4`。
- **shell 命令头不能再配 content 注入**：`cmd: 'cmd /c ...'` / `cmd: 'bash -c ...'` 这类配置现在编译报错（退出码 `3`）。
- **含 `%` 的工作区路径 + `.cmd/.bat` agent 会被拒绝执行**：给出可执行建议（换个不含 `%` 的路径）；`.exe` 与 `powershell -File` 不受影响。
- **judge 抛异常现在是"节点失败"而非"verdict 闸"**：落 `node_failed` 并按既有 `retry` 闸停住等人工；失败原因在账本 `node_failed.payload["reason"]`——**第七轮起 `run`/`status` 概览会直接打印失败归因**（`失败: <node>（exit_code=… reason=… timed_out=…）`，见 T020j），不必再翻账本反推。

### M1 结构拆分批次（第六轮，2026-10-18，已完成）

**目标**：把超线文件按职责拆小，**纯搬移、行为零变化**（"改一处功能只碰一个文件"）。定量口径：全套 **455 条不变**；行数按字节级 `splitlines()` 计。

**`src/`（Lane E）**

| 文件 | 前 → 后 | 新模块（真实行数） |
|---|---|---|
| `kernel/todo_projection.py` | 457 → 166 | `kernel/todo_user_marks.py`(351) |
| `cli.py` | 451 → 389 | `kernel/state_summary.py`(49)、`kernel/workspace_session.py`(63) |
| `kernel/hooks_assembly.py` | 391 → 184 | `kernel/evidence_conversion.py`(225) |
| `kernel/engine_node_executors.py` | 388 → 256 | `kernel/decision_executor.py`(176) |
| `kernel/engine_gates.py` | 366 → 384 | （吸收共享的 `append_node_failed`，避免循环导入） |

**`tests/`（Lane F）**

| 原文件 | 前 → 拆为 |
|---|---|
| `test_cli.py` | 780 → 103 + `test_cli_resume.py`(267) + `test_cli_status_approval_todo.py`(144) + `test_cli_ledger_corruption.py`(118) + `test_cli_workspace_identity.py`(103) |
| `test_ledger.py` | 499 → 178 + `test_ledger_projection.py`(151) + `test_ledger_incremental_verify.py`(119) + `test_ledger_edge_activation_guard.py`(70) |
| `test_mermaid_parser.py` | 577 → 240 + `test_mermaid_frontmatter.py`(194) + `test_mermaid_agent_command_splitting.py`(176) |
| `test_compiler_validator.py` | 531 → 180 + `test_compiler_golden_baseline.py`(146) + `test_compiler_edge_constraints.py`(123) + `test_compiler_output_path_escape.py`(114) |
| `test_todo_projection.py` | 463 → 228 + `test_todo_projection_marker_instructions.py`(209) |

跨文件共用的测试辅助上移到**不被 unittest 收集**的支持模块（文件名不带 `test` 前缀）：`cli_harness_support.py`(142)、`ledger_test_support.py`(67)、`mermaid_parser_support.py`(27)、`compiler_validator_support.py`(51)、`todo_projection_support.py`(87)。每个新测试文件既能被 `discover` 收集，也能 `python tests/<file>.py` 直跑（18/18 全绿）。

**契约保持**：拆分后所有既有模块级公共名在**原路径**仍可导入（`todo_projection` / `hooks_assembly` / `engine_node_executors` 保留再导出；三个文件的 `__all__` **逐字未改**）。私有助手在提取时改为公共名（`_execute_decision` → `execute_decision`、`_trip_gate` → `trip_decision_gate`、`_append_node_failed` → `append_node_failed`、`_open_workspace_state` / `_workspace_ledger_path` / `_workspace_is_initialized` / `_append_human_resolution` / `_print_state_summary` 去掉下划线），属提取的一部分、**非公共 API 变更**。

**验收证据**：`python -m unittest discover -s tests` → **455 tests OK**（多次复跑一致）；`-W always::ResourceWarning` → 455 OK 且 `ResourceWarning` **归零**；用 `TestLoader.discover` 对比拆分前后**叶子测试方法名多重集 `identical: True`**（无增 / 无删 / 无改名；`module.Class` 段随文件归属变化是拆分的必然，与既有 `test_ledger_readonly_view.py` 等先例一致）；全仓 108 个文件 **0 个 BOM、0 个 CRLF**；行数扫描当时余 `tests/test_kernel_engine_gates.py`(428)、`tests/test_kernel_judge.py`(424) 两个待拆 → 见下方收官记录。纯搬移核验：改文件 `git diff --stat` 为 **75 增 / 749 删**（删多于增 = 搬出特征），AST 级字符串字面量与 def/class/赋值名字多重集比对无删减。

**`tests/` 收官（第六轮下半，同日）**

| 原文件 | 前 → 拆为（真实行数，括号内为测试条数） |
|---|---|
| `test_kernel_engine_gates.py` | 428 → 157(5) + `test_kernel_engine_gates_recovery.py`(104, 2) + `test_kernel_engine_terminal_state_guards.py`(205, 4) |
| `test_kernel_judge.py` | 424 → 184(14) + `test_kernel_judge_verdict_extraction.py`(113, 15) |

支持模块（同样不被收集）：`kernel_engine_gates_support.py`(41)、`kernel_judge_support.py`(174)。收官后**全仓 113 个文件无一个超 400 行**。纯搬移证据：AST **方法级**比对 `54 → 54`，`missing / extra / changed` 全空（方法正文逐字未动）；叶子测试方法名多重集 `identical: True`；455 tests OK 且 `ResourceWarning` 归零。另清掉一处**既存**未用导入（`tests/test_kernel_engine.py` 的 `write_todo`，自 HEAD 起即未使用，非本次拆分引入）。

### M1 语义定稿批次（第七轮，2026-10-19，已完成）

**控制面（Lane G）**
- [x] **退出码 `2` 定稿为"零功能变更的契约"**：不新增 `halt` 子命令；把保留语义钉成测试——账本一旦含 `flow_halted`，`resume` 拒绝恢复（退 `2`、stderr 说明、**不落 `flow_resumed`**）、`status` 打印 `flow: halted` 并退 `2`、`run` 对已 halted 账本退 `2`。顺带修掉 `resume` halted 分支此前返回 `EXIT_WAITING`(`1`) 与退出码表 `2` 不一致的问题（`cli.py` 399 行——**已无余量，下次改 CLI 前先抽 argparse 面**）。
- [x] **T020j 失败归因**、**T020k join 前驱不可达**、**T020i 热路径实测并决策保留**、**恢复增量盲区公开为契约**：详见上方台账行。
- [x] **`split_agent_command` 改 fail-loud**：抽出 `compiler/agent_command_splitter.py`(201 行)，`mermaid_parser.py` 396 → 258 并再导出**同一对象**（`__all__` 逐字未改）。

**判定/证据面（Lane H）**
- [x] **T020a 判定 usage 入账**：`judge.py` 新增 `JudgeDispatchFacts`（只回传事实、不落盘，守单一写者纪律）；`decision_executor.py` 与 task 路径**同口径**落 `agent_exited`（`tokens` 仅 >0 写、幂等键 `{run}-{node}-agent-exited-{attempt}`、位置在 `verdict` 之前）；用量复用同一个 `extract_usage_tokens`，**结构化结果与判定协议自己的结构化通道只取其一**（相加即重复计数），`tokens` 跨重试累加。**有意副作用**：判定花费从此进 `token_spent()` 与 budget 闸。
- [x] **T020l verdict.edge**：详见上方台账行。
- **T020f**：测试侧**无重复适配器可去**；台账原文那条实指**生产侧** `fallback_adapter.py`（长尾 CLI 下 task 路径 `tokens` 恒 0）→ 另开工单，**维持开放**。
- **T020o**：部分闭合（详见台账行；引擎侧仍缺 2 条）。

**验收证据**：`python -m unittest discover -s tests` → **491 tests OK**（455 + 控制面 20 + 判定面 16；两条线的新测试都被收集，条数只增不减）；`-W always::ResourceWarning` → 491 OK 且 `ResourceWarning` **归零**；全仓 **119 文件 0 BOM / 0 CRLF / 无超 400 行**；两条线**在同一文件 `tests/test_kernel_engine.py` 上的改动经核对共存、互不覆盖**。每条修复都做了"回退即变红"：控制面 —— 条目1 两红 / 条目2 两错 / 条目3 一红 / 条目5 三红 / 条目6a 四红 / 条目6b 一红；判定面 —— T020a 三红（回退 judge 侧另加一条收集错）、T020l 两红。

**本批新增待办（另开，勿与已闭合项混记）**
- **T020a 窄边界语义**：判定投递两轮皆超时（tripped）后、`verdict` 闸之前进程崩溃时，恢复路径会读到那条 `agent_exited(timed_out=True)` 而走**超时分支**而非 retry 闸。语义上更正确（判定 agent 确实超时了），但**无用例覆盖**，需一条用例 + 一次语义确认（倾向上维持现状）。
- **T020f 生产侧**：`fallback_adapter.py` 从任意 agent 输出提取 usage（task 路径 `tokens` 恒 0）；判定路径已由 T020a 顺带缓解。
- **退化 win32 切分**：无 ctypes 时仍有约 9.5% 病态输入被静默错切（选项：忠实复现 `CommandLineToArgvW`，或原生不可用时直接拒绝切分）。
- **大图性能（若开）**：`outgoing_edges` 建索引 + `has_edge_been_taken` 批量读 + `describe_join_blocking` 的 O(J·E) 一并处理。

#### 本轮行为变更（面向用户，升级前请读）

- **`verdict.payload["edge"]` 由「目标节点名」改为「整数＝声明序号」**：跨版本不兼容**仅限账本解读**（外部按名字读该字段的脚本会失效）；重放 / 续跑 / 幂等 / 工作区身份**零变化** → **不需要**换新工作区。
- **已 halted 的账本：`resume` 现在退 `2`**（此前退 `1`），`status` 也退 `2`。
- **判定调用的 token 花费开始入账**：预算闸现在会被**判定花费**触发（此前判定花费不可见）。
- **病态引号序列不再静默错切**：`split_agent_command` 对「引号段落内出现 `""`」抛 `ValueError`（win32 上原生/回退对拍不一致也抛）；此前是静默按回退切分。
- **失败节点在 `status` / `run` 概览直接显示归因**：`失败: <node>（exit_code=… reason=… timed_out=…）`。
- **join 永久阻塞显示为异常悬挂**：「join 阻塞 … 已 failed，永远不会到达」——只是**展示诊断**，不设闸、不落事件。

### M1 审计遗留（第二轮新发现，并入 M2 处理）

| 条目 | 现象 | 位置 |
|---|---|---|
| out= 放行 NTFS ADS（✅ 第四轮已修） | `a.txt:stream` 形式未被拦（非路径逃逸，但可指同目录数据流） | `compiler/validator.py` |
| cmd 垫片参数级注入面（✅ 第五轮已修） | 模板参数本身含 `&`/`%`/`"` 时仍被 cmd.exe 重分词（暴露面与修复前同级，未新增） | `adapters/cmd_line_safety.py`（新增）、`adapters/base.py` 启动计划 |
| shell 命令头 content 模式理论绕过（✅ 第五轮已修） | `cmd`/`powershell` 命令头走 content 模式的理论绕过面（默认多行 packet 不可触发） | `adapters/registry.py` 注入策略 + `compiler/validator.py` 报告 |
| path 模式不中和 `%VAR%`（✅ 第五轮已修） | 仅当工作区路径含 `%…%` 时暴露 | `adapters/cmd_line_safety.py` |
| 数值无上界（✅ 第四轮已修） | `timeout` 可溢出为 `inf`；`budget_tokens`/`retry` 无上限 | `compiler/limits.py`（新增）、`compiler/mermaid_edge_label.py`、`compiler/mermaid_parser.py` |
| `timeout:` 边可指向 `human`（✅ 第四轮已修） | 现只拒 join/decision | `compiler/validator.py` |
| 退出码 `2` 无生产者 | M1 无 `flow_halted`/`checkpoint` 写入方，`is_halted()` 恒 False、相关分支为死代码 | `cli.py`、`state/flow_state.py` |
| CLI 单一 `except OSError` 误报（✅ 第四轮已修） | 写盘失败被报成"无法读取流程文件" | `cli.py` |
| 勾选未到达的人审节点即判 done（✅ 第四轮已修） | 勾选尚未到达 `awaiting_human` 的人审节点会被立即判 done | `kernel/todo_projection.py` |
| judge 异常后判节点进死胡同（✅ 第五轮已修） | judge 异常兜底后判定节点无再审路径，resume 不可恢复 | `kernel/engine_node_executors.py` |
| usage 重复计数（✅ 第三轮已修） | usage 同时含数字叶子与 `total_tokens` 时重复计数 | `kernel/engine_evidence.py` |
| 只读判定代理带写副作用（✅ 第三轮已修） | 只读判定代理构造 `EventLedger` 会截断账本尾行 | `kernel/hooks_assembly.py`、`state/ledger.py` |
| 引擎账本句柄泄漏（✅ 第三轮已修） | `run_flow` 抛异常时内部账本句柄不关闭（测试可见 `ResourceWarning: unclosed file ledger.jsonl`） | `kernel/engine.py` |
| 异常账本身份校验被跳过（✅ 第五轮已修） | 账本非空但无 `flow_started` 时身份校验跳过、直接放行 | `kernel/engine.py` |
| 同源同目标多边被幂等合并（✅ 第五轮已修） | 多条无条件边被幂等键合并，丢失"哪条 when 边走"的审计事实 | `kernel/engine_gates.py` |
| 恢复增量盲区 | 同进程内"原地改写已确认前缀"不可被增量路径察觉（跨实例首次仍全量、可检出） | `state/ledger.py` |
| `split_agent_command` 语义边界 | 空参数丢弃的语义将来若需传空参数会变；手写回退实现对病态引号序列与原生可能不一致 | `compiler/mermaid_parser.py` |
| 文件超 ~400 行（✅ 第六轮已全部拆分） | 真实行数超线的既有文件（口径已修正，见下）。第六轮已把 `src/` 4 个 + `tests/` 7 个**纯搬移**拆到 ≤400（含下半收官），**全仓 113 个文件已无超线文件** | 见「M1 结构拆分批次（第六轮）」 |

> **行数口径修正（2026-10-18）**：此前用 PowerShell `Measure-Object -Line` 统计，该 cmdlet **跳过空行**，全仓行数被低估约 15%（例：`cli.py` 记 360、实为 451）。改用字节级 `splitlines()` 重测后得左侧真实清单。第四轮起，新代码一律落到新模块/新测试文件（`compiler/limits.py`、`test_compiler_limits.py`、`test_cli_io_errors.py`、`test_todo_projection_human_guard.py`），未再给超线文件加厚。

> **存量台账复核（T020i–T020p）**：`i` 已于第七轮**实测并决策保留**（见下）、`j`/`k`/`l` 已于第七轮闭环（见下）、`m` 维持开放（下一波处理）、`n` 已于第六轮完成（见下）、`o` 部分闭合（第七轮补齐 evidence 面 3 条，引擎面仍缺 2 条）、`p` 已于第三轮随根因修复关闭（见下）。

### M1 审计遗留（新发现，并入 M2 处理）

- [x] **T020i 内核热路径 O(E²)（✅ 第七轮实测并决策保留）**：`ledger.entries` 每次全量拷贝，长流程下判定/证据装配呈二次复杂度。
  - **实测（第七轮）**：链式图（全 done、无 append）50 边 **0.065ms** → 800 边 **8.5ms**（每翻倍 ≈3.2–4.0×，**确为二次**）；星形图含 `edge_taken` 落账 50 次 **108.9ms**、800 次 **8521ms**（这条由**每次落账一次 fsync** 主导，**不作为第二处二次项的证据**）。结构性二次项定位在 `outgoing_activatable_edges → outgoing_edges` 的**逐节点全边扫描**；`has_edge_been_taken` 另加 `ledger.entries()` 的 O(N) 拷贝 + O(E) 幂等扫描。
  - **决策：保留不改**（真实流程图规模极小，落账 fsync 才是主成本）。若将来开大图性能工单，应与 `outgoing_edges` 建索引、`has_edge_been_taken` 批量读、`describe_join_blocking` 的 O(J·E) 一并处理。
  - （2026-10-16 复核：本轮的**账本链增量校验 + 快照缓存**已覆盖**快照/恢复路径**的重复全量开销。）
- [x] **T020j 失败归因写进 payload（✅ 第七轮已修）**：`node_failed` 不再只写通用 reason——现写 `{exit_code, timed_out, reason}`，`reason ∈ {timed_out, nonzero_exit, missing_outputs, dispatch_exception, agent_unavailable, interrupted_by_restart}` 或 judge 异常串；`run`/`status` 概览直接打印 `失败: <node>（exit_code=… reason=… timed_out=…）`，不必再反推邻接事件。
- [x] **T020k join 前驱不可达需提示（✅ 第七轮已修，口径＝展示层诊断）**：`kernel/state_summary.py::describe_join_blocking` 区分「join 等待 …（正常推进中）」与「join 阻塞 … 已 failed，永远不会到达」。**这是展示诊断，不落事件、不设闸**，流程仍停在等待点等人工——刻意不与 design §5.3 的闸语义混用。
- [x] **T020l verdict.edge 口径统一（✅ 第七轮已落地）**：统一为**命中边在 `flow_ir.edges` 中的声明序号**（与 `edge_taken` 幂等键 `index` 同口径，复用 `_edge_position_in_flow` 保证不漂移）；tripped / 未命中记 `null`。**跨版本不兼容**（`edge` 由字符串＝目标节点名 → 整数）：但投影层不读该字段 → 重放 / 续跑 / 幂等 / 工作区身份**零变化**，**无需换新工作区**，只在升级说明写一句口径变更即可。design.md §4.4 / §5.2 已同步。
- [ ] **T020m 审计文件名去实例耦合**：审计文件名含进程/实例信息，跨 run 不稳定。
- [x] **T020n 内核模块再拆分（✅ 第六轮已完成）**：`hooks_assembly.py`(391) / `engine_node_executors.py`(388) 已把"证据转换"段与 decision 执行器抽为独立模块——`evidence_conversion.py`(225) / `decision_executor.py`(176)，原文件降到 184 / 256，公共名在原路径再导出（契约零变化）。
- [ ] **T020o 测试缺口补齐（🟡 第七轮部分闭合）**：已闭合——`missing_outputs` 分支（`tests/test_kernel_engine_failure_attribution.py`，含缺件清单）、T012 桩行为验收（`tests/test_stub_generator.py`）、闸后停住/恢复的引擎侧直测（`test_kernel_engine_gate_exemption.py` / `test_kernel_judge_failure_terminal.py` / `test_kernel_engine_terminal_state_guards.py`）、evidence 面 3 条（`tests/test_kernel_hooks_assembly.py::HooksAssemblyContractGapTestCase`：缺件 `outputs_present=False`、多上游合并产物键带节点前缀、join 上游上溯展开逐项核对）。**仍缺 2 条（引擎侧，待办）**：① `@pause` 的**引擎级**直测（现有只有 CLI 级与投影级，全部为 `<!-- @pause -->` 行尾注释形式）；② 「`exit≠0` + `semantic_done=True` + 产物齐」的**节点级**组合（适配层已钉，引擎节点级无测，而 `succeeded = not timed_out and (exit_code == 0 or semantic_done)` 正靠它成立）。
- [x] **T020p 测试卫生**：全套偶发 `ResourceWarning: unclosed file … ledger.jsonl`。（2026-10-16 复核定位两条产品根因：`kernel/engine.py` 引擎异常时账本句柄泄漏、`kernel/hooks_assembly.py` 只读判定代理自建 `EventLedger` 带写副作用。**2026-10-18 两条均已修复**；剩余噪声来自测试辅助 `reopened_ledger()` 未关句柄，已统一 `addCleanup` 并调整清理顺序修掉。复验：`python -W always::ResourceWarning -m unittest discover -s tests` → **386 OK 且 `ResourceWarning` 归零**。）

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
