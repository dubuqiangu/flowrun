# FlowRun M1 实现契约（并行开发的接口冻结件）

> 本文冻结模块边界与数据形状。三个并行开发域（编译器/状态层/适配层）以本文为准；任何要改本文的行为必须先回 orchestrator。

## 0. 全局技术约束

- **语言**：Python 3.11+，**仅标准库**（json / re / asyncio / subprocess / hashlib / pathlib / argparse / dataclasses / unittest / tempfile / shutil / time）。M1 禁止第三方依赖（含 pytest、pyyaml、mermaid 库——frontmatter 与 Mermaid 子集全部手写解析）。
- **平台**：Windows 优先（开发机 win11），Linux 兼容（subprocess 用 `creationflags`/`start_new_session` 分支或直接不依赖进程组）。
- **测试**：stdlib `unittest`，每域自带 `tests/test_<模块>.py`，全部测试必须可零配置直跑（`python -m unittest discover -s tests`）。
- **命名**：见名知意，禁止单字母/缩写歧义/数字后缀。单文件超 ~400 行或多职责必须拆。
- **入口**：`src/flowrun/cli.py` 只做装配（argparse → 各模块调用），不承载业务逻辑。

## 1. 目录与归属（写权限分域，禁止越界写）

```
flowrun/
├── design.md / tasks.md / interfaces.md      # orchestrator
├── src/flowrun/
│   ├── ir/schema.py            # 域A：IR 数据结构（全项目地基）
│   ├── compiler/
│   │   ├── mermaid_parser.py   # 域A：flow.mmd → 原始解析树
│   │   └── validator.py       # 域A：解析树 → FlowIR + 校验错误(带行号)
│   ├── state/
│   │   ├── ledger.py           # 域B：事件账本
│   │   └── snapshots.py        # 域B：快照与恢复
│   ├── adapters/
│   │   ├── base.py             # 域C：适配器接口 + 双信号退出监视器
│   │   ├── registry.py         # 域C：声明式注册 + which 探测
│   │   ├── claude_code.py      # 域C
│   │   └── opencode_cli.py     # 域C
│   ├── kernel/                 # 域D（M1 第二批）：engine/judge/todo_projection/stub_generator
│   └── cli.py                   # 域D 最后装配
├── tests/                       # 各域自带测试文件
└── examples/flow.mmd            # 域A 维护
```

## 2. 域A · IR schema（`ir/schema.py`）

dataclass 定义（字段名冻结，全项目按此引用）：

```python
@dataclass
class NodeSpec:
    node_id: str          # "T001"（用户图里的 id）
    kind: str             # "task" | "decision" | "human" | "join"   # join 为隐式节点：多入边节点编译时若任一入边带 when:all 则标记（M1 实现兼容写在出边上，见 design.md §4.1）
    label: str            # 显示名（去掉 ;; 配置后的文本）
    agent: str | None    # agent= 声明，None → 用 frontmatter defaults
    outputs: list[str]    # out= 产物契约（相对路径列表），可为空
    line_number: int      # 源图行号（报错用）

@dataclass
class EdgeSpec:
    source_id: str
    target_id: str
    when: str | None      # None=无条件；"all"=barrier；其他=自然语言谓词
    retry: int             # 0=无
    timeout_seconds: float | None
    line_number: int

@dataclass
class FlowIR:
    run_name: str
    default_agent: str
    budget_tokens: int
    agent_registry: dict[str, AgentCommandSpec]   # name -> {command(list[str]), resume_mode(str|None)}
    nodes: dict[str, NodeSpec]                     # node_id -> spec，含隐式 join
    edges: list[EdgeSpec]
    entry_node_ids: list[str]                       # 无入边节点
    def to_dict(self) -> dict: ...                  # JSON 序列化（golden 基准用）
```

frontmatter 契约：YAML 子集，仅支持 `key: value`、一层缩进映射、`{}` 内联映射、`[]` 内联数组、引号字符串——手写解析器实现这个子集即可，不支持嵌套/锚点（遇到即报错）。

## 3. 域B · ledger / snapshots

### ledger.py API（冻结）

```python
class EventLedger:
    def __init__(self, ledger_path: Path): ...
    def append(self, event_type: str, payload: dict, idem_key: str, run_name: str, node_id: str | None = None) -> LedgerEntry
    def verify_chain(self) -> bool            # prev-link 哈希链校验
    def replay(self) -> FlowState             # 幂等去重后重放 → 领域状态投影
    def entries(self) -> list[LedgerEntry]    # 只读
    @classmethod
    def open_readonly(cls, ledger_path: Path) -> EventLedger   # 只读视图：不截断尾行、不持写句柄、append() 抛 RuntimeError

@dataclass
class LedgerEntry:
    seq: int; prev_hash: str; idem: str
    type: str; run: str; node: str | None; ts: float; payload: dict
```

- 哈希：`sha256(prev_hash + canonical_json(entry_without_hash))` 的 hex；canonical_json = `json.dumps(sort_keys=True, separators=(",", ":"))`。
- 事件类型 17 种：`flow_started / node_entered / agent_dispatched / agent_exited / verdict / edge_taken / node_done / node_failed / human_requested / human_resolved / checkpoint / gate_tripped / user_directive / flow_paused / flow_resumed / flow_halted / flow_done`（payload 结构由域D定，域B 只管存取/链/去重/重放投影的**通用骨架**）。**M1 生产口径**：实际落账 14 种，`flow_paused` / `flow_halted` / `checkpoint` 无生产路径（投影层预留），见 design.md §4.4。
- `FlowState`：重放产出的只读投影（dict 包装即可），提供 `node_status(node_id) -> str`、`completed_nodes() -> set`、`is_paused()` 等查询方法——具体查询语义按 design.md §5.1 状态机。

### snapshots.py API（冻结）

```python
class SnapshotStore:
    def __init__(self, snapshot_dir: Path, every_n_events: int = 20): ...
    def maybe_snapshot(self, ledger: EventLedger, flow_state: FlowState) -> Path | None
    def load_latest(self) -> tuple[FlowState, int] | None   # (状态, 已覆盖的seq)
```

恢复实际契约：`recover_flow_state(ledger, snapshot_store)` = 最新快照投影 + 对快照点之后的条目经 `FlowState.merge_from_events()` 增量合并（`EventLedger.replay()` **无 seq 参数**，是整账重放）。恢复时做防篡改双重核对：快照覆盖点的链校验 + `head_hash` 比对。写入用 temp 文件 + `os.replace` 原子替换。

**增量实现细节（M1 已落地，磁盘格式与恢复等价性不变）**：链校验只校验**新增条目**——实例内已确认的前缀不重复读盘/重算哈希，文件比上次校验点更短时回退全量，新实例首次调用永远全量；最新快照记录做进程内缓存 + 低成本失效判据。已知盲区：同进程内"原地改写已确认前缀"增量路径察觉不到（跨实例首次仍走全量、可检出，见 tasks.md 台账）。

## 4. 域C · 适配层

### base.py API（冻结）

```python
class DispatchResult:
    exit_code: int | None
    timed_out: bool
    semantic_done: bool      # 双信号：语义完成（如流式结果出现 end_turn/result 消息）
    stdout_path: Path        # 原始输出落盘（审计）
    duration_seconds: float

class AgentAdapter(Protocol):
    name: str
    async def dispatch(self, packet_path: Path, agent_command: list[str], work_dir: Path,
                       timeout_seconds: float | None) -> DispatchResult: ...
```

> **实现超出本冻结面（M1 事实，待并入正式契约）**：`DispatchResult` 另含 `structured_result`（适配器从输出提取的结构化结果）与 `stderr_path`（stderr 审计落盘路径）；`EventLedger` 另含只读 API `last_seq` / `head_hash` / `close()` 与 `with` 上下文管理器支持。**M2 追加**：`EventLedger.open_readonly()` 为面向旁路读取的只读视图（写者构造时截断崩溃尾行，只读视图一律不截断、不持写句柄），判定等只读路径一律走该视图。

- **双信号退出监视器**在此实现：asyncio task `select` 语义——进程退出 vs 语义完成信号 vs 超时，三者竞速；语义完成先到则优雅等待进程退出（宽限期 30s），超期 kill 并记 `timed_out=False, semantic_done=True`。
- 判定"语义完成"是**适配器私有**逻辑：claude_code 监听 stream-json 的 `result` 消息；opencode 监听事件流到 session idle。

### registry.py API（冻结）

```python
@dataclass
class AgentCommandSpec:      # 也放 ir/schema.py，域A定义，域C引用
    command: list[str]      # 如 ["claude", "-p", "--output-format", "stream-json"]
    resume_mode: str | None # "session"=可用会话续跑；M1 仅记录不实现

def probe_available(agent_registry: dict[str, AgentCommandSpec]) -> dict[str, bool]   # shutil.which(command[0])
```

占位符机制：命令模板里允许 `__FLOW_PACKET__`（替换为 packet 正文的注入位置）或 `__FLOW_PACKET_PATH__`（替换为 packet 绝对路径）；**模板内出现任一占位符时不再追加末尾参数**。投递方式默认（模板无占位符）：`packet` 作为最后位置参数注入（`claude -p "$(cat packet)"` 语义）。

**注入模式（M1 安全约束，实测取证）**：命令头经 `shutil.which` 解析为垫片（`.cmd` / `.bat` / `.ps1`；win32 上 `which` 未命中时按候选扩展集 `.exe/.cmd/.bat/.ps1` 做**有界 PATH 扫描**兜底，**探测与派发使用同一入口**）时，内核自动改用 **path 模式**——`__FLOW_PACKET__` 与 `__FLOW_PACKET_PATH__` 一律替换为路径，packet **正文一个字节都不进 argv**（命令行会被解释器重新分词，正文里的 `"` / `&` / `|` 等于把 packet 内容当命令执行）。因此**垫片型 agent 的模板必须显式消费路径占位符**（见下方 opencode 例），否则末位追加的只是一个路径字符串、agent 拿不到正文。

两种垫片的启动方式不同：

- `.cmd` / `.bat`：正文进文件，启动时把**整条命令行**（由 `subprocess.list2cmdline` 生成）交给 `create_subprocess_shell`，以规避"垫片路径与参数都含空格时 cmd 引号剥离导致垫片根本不执行"。
- `.ps1`：与 `.cmd/.bat` 一样走 path 模式，启动方式为 `powershell -NoProfile -ExecutionPolicy Bypass -File <shim>`。

命令头在 win32 上不可解析时抛 `CommandHeadUnresolvableError`（**继承 `FileNotFoundError`**）并给可读提示，不再让 `WinError 2` 穿透。

**`split_agent_command`（win32 命令分词）**：采用 `CommandLineToArgvW` 语义（优先 ctypes 原生调用，不可用时回退到按 MS 规则的等价手写实现）——引号内的 `&`、`;`、`|`、`%` 是**字面量**，空词丢弃，未闭合引号仍报错；**非 win32 分支行为不变**（仍走 `shlex`）。

claude/opencode 两个适配器 + registry 的声明式注册，具体命令按 design.md §2.2 调研结论（现场已查证）：
- claude：`claude -p --output-format stream-json`
- opencode：`opencode run --format json --file __FLOW_PACKET_PATH__`（Windows 上 npm 垫片走 path 模式，正文经 `--file` 投递；`--file` 语义待真机采样复核，见 tasks.md T020g）

## 5. 域D · 内核（第二批，等 A/B/C 合并后开发；此处仅冻结其对上游的使用方式）

- engine/judge/todo_projection/stub_generator 只 import `ir.schema`、`state.ledger`、`state.snapshots`、`adapters.base`、`adapters.registry`，**不得**绕过接口直接读上游内部结构。
- 判定 LLM 调用 = 通过适配器投递判定 packet（`__FLOW_PACKET__` 注入 JSON Schema 约束的判定提示），M1 不接任何 LLM SDK。
- packet.md 格式按 design.md §4.3。

## 6. 冒烟口径（T013）

M1 端到端验收（口径修正）：冒烟测试用**与 design.md §4.1 样例图同构的自造图**（`compose_smoke_flow_document`，注册假 agent、为规避回边调整了失败分支指向）驱动 fork/join/判定/人审，ledger 全量重放一致——**不直读** `examples/flow.mmd`（其 agent cmd 是真 CLI，CI 不可跑）。真 agent 冒烟由用户在本机跑（需要本机装 claude/opencode CLI）。

## 7. 并行开发规则

- 各域只写自己的目录 + 自己的测试文件；`examples/` 只有域A写；发现上游接口不合用时**不要越界修**，在返回报告中列出，由 orchestrator 裁决。
- 所有对外输入（packet、agent 输出、todo 用户标记）按不可信处理：只进结构化解析，绝不 eval/执行。
- 每个交付报告必须含：模块清单、公共 API 实际签名（如有偏差说明原因）、测试清单与通过数、发现的上游问题。
