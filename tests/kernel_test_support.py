"""域D2 测试共享支撑：FlowIR 构造器、内核 hook 可编程 stub、假 agent 子进程投递。

被 test_kernel_engine*.py 复用（文件名不带 test_ 前缀，避免被 unittest 收集）。
域D1 的 judge/packet_builder 不 import：verdict 与 packet 全部用本文件的 stub。
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from flowrun.adapters.base import DispatchResult  # noqa: E402
from flowrun.ir.schema import (  # noqa: E402
    NODE_KIND_DECISION,
    NODE_KIND_HUMAN,
    NODE_KIND_JOIN,
    NODE_KIND_TASK,
    EdgeSpec,
    FlowIR,
    NodeSpec,
)
from flowrun.kernel.engine import EngineHooks  # noqa: E402
from flowrun.state.ledger import EventLedger, LedgerEntry  # noqa: E402

FAKE_AGENT_SCRIPT_PATH = PROJECT_ROOT / "tests" / "fixtures" / "fake_flow_agent.py"
DEFAULT_RUN_NAME = "release-login"
DEFAULT_BUDGET_TOKENS = 1_000_000
DEFAULT_AGENT_NAME = "fake-agent"


# ---------------------------------------------------------------- FlowIR 构造


def make_node(
    node_id: str,
    kind: str = NODE_KIND_TASK,
    label: str | None = None,
    agent: str | None = None,
    outputs: list[str] | None = None,
) -> NodeSpec:
    return NodeSpec(
        node_id=node_id,
        kind=kind,
        label=label if label is not None else f"{node_id} 任务",
        agent=agent,
        outputs=list(outputs or []),
        line_number=1,
    )


def make_edge(
    source_id: str,
    target_id: str,
    when: str | None = None,
    retry: int = 0,
    timeout_seconds: float | None = None,
) -> EdgeSpec:
    return EdgeSpec(
        source_id=source_id,
        target_id=target_id,
        when=when,
        retry=retry,
        timeout_seconds=timeout_seconds,
        line_number=1,
    )


def make_flow_ir(
    nodes: list[NodeSpec],
    edges: list[EdgeSpec],
    run_name: str = DEFAULT_RUN_NAME,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
) -> FlowIR:
    incoming_target_ids = {edge.target_id for edge in edges}
    return FlowIR(
        run_name=run_name,
        default_agent=DEFAULT_AGENT_NAME,
        budget_tokens=budget_tokens,
        agent_registry={},
        nodes={node.node_id: node for node in nodes},
        edges=list(edges),
        entry_node_ids=[node.node_id for node in nodes if node.node_id not in incoming_target_ids],
    )


def events_of_type(
    ledger: EventLedger, event_type: str, node_id: str | None = None
) -> list[LedgerEntry]:
    """从账本取某类型（可再按节点过滤）的事件列表，供断言用。"""
    return [
        entry
        for entry in ledger.entries()
        if entry.type == event_type and (node_id is None or entry.node == node_id)
    ]


# ---------------------------------------------------------------- hook stub


@dataclass
class StubJudgeVerdict:
    """判定 hook 返回的 stub verdict（字段与域D1 JudgeVerdict 冻结契约一致）。"""

    tripped: bool = False
    chosen_edge_target: str | None = None
    confidence: float = 0.9
    evidence: list[str] = field(default_factory=list)


def make_dispatch_result(
    exit_code: int | None = 0,
    timed_out: bool = False,
    semantic_done: bool = False,
    usage_tokens: int = 0,
) -> DispatchResult:
    structured_result = (
        {"usage": {"input_tokens": usage_tokens}} if usage_tokens > 0 else {}
    )
    return DispatchResult(
        exit_code=exit_code,
        timed_out=timed_out,
        semantic_done=semantic_done,
        stdout_path=Path("audit") / "stub-stdout.log",
        duration_seconds=0.01,
        structured_result=structured_result,
    )


class KernelHookStubs:
    """内核四个注入 hook 的可编程 stub（packet 由本 stub 写盘，模拟域D1）。"""

    def __init__(self) -> None:
        self.dispatch_script: dict[str, list[DispatchResult]] = {}
        self.verdict_script: dict[str, list[StubJudgeVerdict]] = {}
        self.unavailable_agents: set[str] = set()
        self.dispatch_calls: list[tuple[str, float | None]] = []
        self.dispatch_timeout_log: list[float | None] = []
        self.judge_calls: list[tuple[str, object, float | None]] = []
        self.probe_calls: list[str] = []
        self.snapshot_calls: list[int] = []  # 每次 snapshot 时的 ledger.last_seq
        self.written_packet_nodes: list[str] = []

    def script_dispatch_result(self, node_id: str, *results: DispatchResult) -> None:
        self.dispatch_script.setdefault(node_id, []).extend(results)

    def script_verdict(self, node_id: str, *verdicts: StubJudgeVerdict) -> None:
        self.verdict_script.setdefault(node_id, []).extend(verdicts)

    # ---- EngineHooks 四个注入点 ----

    def build_task_packet(self, flow_ir, node, work_dir, packet_dir) -> Path:
        packet_dir = Path(packet_dir)
        packet_dir.mkdir(parents=True, exist_ok=True)
        packet_path = packet_dir / f"{node.node_id}.md"
        packet_path.write_text(
            f"---\nnode: {node.node_id}\nrun: {flow_ir.run_name}\n"
            f"contract: [{', '.join(node.outputs)}]\n---\n{node.label}\n",
            encoding="utf-8",
        )
        self.written_packet_nodes.append(node.node_id)
        return packet_path

    def probe_agent(self, agent_name: str) -> bool:
        self.probe_calls.append(agent_name)
        return agent_name not in self.unavailable_agents

    async def dispatch_task(self, node, packet_path, timeout_seconds) -> DispatchResult:
        self.dispatch_calls.append((node.node_id, timeout_seconds))
        self.dispatch_timeout_log.append(timeout_seconds)
        scripted_results = self.dispatch_script.get(node.node_id)
        if scripted_results:
            return scripted_results.pop(0)
        return make_dispatch_result()

    async def judge_decision(
        self, flow_ir, decision_node, evidence_pack, work_dir, packet_dir, timeout_seconds
    ) -> StubJudgeVerdict:
        self.judge_calls.append((decision_node.node_id, evidence_pack, timeout_seconds))
        scripted_verdicts = self.verdict_script.get(decision_node.node_id)
        if scripted_verdicts:
            return scripted_verdicts.pop(0)
        # 缺省 verdict：选声明顺序第一条出边（模拟判定命中）
        first_target = next(
            (
                edge.target_id
                for edge in flow_ir.edges
                if edge.source_id == decision_node.node_id
            ),
            None,
        )
        return StubJudgeVerdict(chosen_edge_target=first_target)

    def snapshot(self, ledger, flow_state) -> None:
        self.snapshot_calls.append(ledger.last_seq)

    def build_hooks(self) -> EngineHooks:
        return EngineHooks(
            build_task_packet=self.build_task_packet,
            probe_agent=self.probe_agent,
            dispatch_task=self.dispatch_task,
            judge_decision=self.judge_decision,
            snapshot=self.snapshot,
        )


class SubprocessAgentHookStubs(KernelHookStubs):
    """用真实子进程跑 tests/fixtures/fake_flow_agent.py 的投递 stub。

    模拟生产适配器：packet 内容作为最后位置参数注入、stdout 末行 JSON 解析为
    structured_result、超时用 asyncio.wait_for 裁决。work_dir 取 packet 所在
    packets/ 目录的父目录（dispatch hook 冻结签名不含 work_dir）。
    """

    def __init__(self, agent_mode_by_node: dict[str, str]) -> None:
        super().__init__()
        self.agent_mode_by_node = dict(agent_mode_by_node)

    async def dispatch_task(self, node, packet_path, timeout_seconds) -> DispatchResult:
        packet_path = Path(packet_path)
        self.dispatch_calls.append((node.node_id, timeout_seconds))
        command = [
            sys.executable,
            str(FAKE_AGENT_SCRIPT_PATH),
            "--mode",
            self.agent_mode_by_node.get(node.node_id, "normal"),
            packet_path.read_text(encoding="utf-8"),
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=packet_path.parent.parent,
        )
        timed_out = False
        try:
            stdout_bytes, _stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            stdout_bytes, _stderr_bytes = await process.communicate()
        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        structured_result: dict = {}
        for output_line in reversed(stdout_text.splitlines()):
            if output_line.strip().startswith("{"):
                try:
                    structured_result = json.loads(output_line)
                except json.JSONDecodeError:
                    pass
                break
        exit_code = None if timed_out else process.returncode
        return DispatchResult(
            exit_code=exit_code,
            timed_out=timed_out,
            semantic_done=bool(structured_result),
            stdout_path=Path("audit") / f"{node.node_id}-stdout.log",
            duration_seconds=0.05,
            structured_result=structured_result,
        )
