"""FlowRun 域D2 · run 子目录布局的解析（T022 多 run 隔离）。

背景：M1/M2 的工作区是"扁平"的——`ledger.jsonl` / `snapshots/` / `packets/` /
`audit/` / `todo.md` 全部直接位于工作区根，且内核强制"一个工作区只服务一个 run"。
T022 引入多 run 隔离，但**不能打断既有工作区**，因此采用双布局：

- **legacy（旧布局）**：工作区根存在 `ledger.jsonl` → 沿用扁平路径，零迁移。
  这种工作区仍然只服务一个 run，身份校验语义与 M2 完全一致。
- **namespaced（新布局）**：工作区根**没有** `ledger.jsonl` → 每个 run 独占
  `<workspace>/<run>/`，其中 `ledger.jsonl` / `snapshots/` / `packets/` /
  `audit/` / `todo.md` 都在该子目录内；`AGENTS.stub.md` 属于**工作区级**资产
  （它描述的是"这个目录里怎么工作"），仍留在工作区根，各 run 共享同一份。

判据只用"根的 ledger.jsonl 是否存在"这一个事实，因为
`workspace_is_initialized` 早就以此为唯一判据（README/interfaces 已公开），
复用同一事实可保证"已初始化的工作区"与"走哪套布局"永远一致，不会出现
"初始化判定走 A、路径拼接走 B"的漂移。

本模块只做路径推导，不读不写账本，不做任何 IO。

**为何默认走 namespaced 而不是"根平铺 + run 子目录并存"**：每个 run 各自一份账本
是用户裁定（哈希链互不干扰、单实例写者语义不变、恢复/重放逻辑零改动）。既然
账本已经 per-run，与之同级的 snapshots/packets/audit/todo 也 per-run 最一致，
否则"这份 packet 属于哪个 run"要靠文件名前缀猜。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "LEDGER_FILENAME",
    "PACKETS_DIRECTORY_NAME",
    "AUDIT_DIRECTORY_NAME",
    "SNAPSHOTS_DIRECTORY_NAME",
    "TODO_FILENAME",
    "AGENTS_STUB_FILENAME",
    "WorkspaceLayout",
    "legacy_layout",
    "layout_for_new_run",
    "layout_for_open_ledger",
    "resolve_layout_for_read",
    "resolve_workspace_layout",
]

LEDGER_FILENAME = "ledger.jsonl"
PACKETS_DIRECTORY_NAME = "packets"
AUDIT_DIRECTORY_NAME = "audit"
SNAPSHOTS_DIRECTORY_NAME = "snapshots"
TODO_FILENAME = "todo.md"
# AGENTS.stub.md 是**工作区级**资产（描述"这个目录里怎么工作"），始终在工作区根，
# 各 run 共享同一份，不随 run 分包。
AGENTS_STUB_FILENAME = "AGENTS.stub.md"


@dataclass(frozen=True)
class WorkspaceLayout:
    """一次运行的目录身份：所有运行期路径都由它派生，不再各处硬编码。

    ``run_root`` 是**该 run 自己的目录**：
    - legacy 布局下它就是工作区根（既有工作区行为零变化）；
    - namespaced 布局下它是 ``<workspace>/<run_name>``。

    ``workspace_root`` 始终是调用方给的工作区根，`AGENTS.stub.md` 等
    工作区级资产用它定位。
    """

    workspace_root: Path
    run_root: Path
    run_name: str
    legacy: bool

    @property
    def ledger_path(self) -> Path:
        return self.run_root / LEDGER_FILENAME

    @property
    def snapshots_dir(self) -> Path:
        return self.run_root / SNAPSHOTS_DIRECTORY_NAME

    @property
    def packets_dir(self) -> Path:
        return self.run_root / PACKETS_DIRECTORY_NAME

    @property
    def audit_dir(self) -> Path:
        return self.run_root / AUDIT_DIRECTORY_NAME

    @property
    def todo_path(self) -> Path:
        return self.run_root / TODO_FILENAME

    @property
    def agents_stub_path(self) -> Path:
        return self.workspace_root / AGENTS_STUB_FILENAME

    def required_directories(self) -> tuple[Path, ...]:
        """运行前必须存在的三个目录（顺序固定，便于测试断言）。"""
        return (self.packets_dir, self.audit_dir, self.snapshots_dir)


def legacy_layout(workspace: Path, run_name: str = "") -> WorkspaceLayout:
    """强制扁平布局（run 根 = 工作区根）：既有工作区与无 run 名的调用点用。"""
    workspace_root = Path(workspace)
    return WorkspaceLayout(
        workspace_root=workspace_root,
        run_root=workspace_root,
        run_name=run_name,
        legacy=True,
    )


def resolve_workspace_layout(workspace: Path, run_name: str) -> WorkspaceLayout:
    """按"根有没有 ledger.jsonl"判定走 legacy 还是 namespaced 布局。"""
    workspace_root = Path(workspace)
    if (workspace_root / LEDGER_FILENAME).exists():
        return legacy_layout(workspace_root, run_name)
    return layout_for_new_run(workspace_root, run_name)


def resolve_layout_for_read(
    workspace: Path, run_name: str | None = None
) -> WorkspaceLayout:
    """读写前的统一解析：无 run 名 → 根；有 run 名 → 根有账本则根，否则 run 子目录。

    这里的"根有账本优先"是**兼容性关键**：已初始化的 legacy 工作区即便被人
    手建了同名 run 子目录，也必须继续读根，否则既有工作区会被静默切走。
    """
    workspace = Path(workspace)
    if run_name is None:
        return legacy_layout(workspace)
    return resolve_workspace_layout(workspace, run_name)


def layout_for_open_ledger(
    ledger_path: Path, workspace: Path, run_name: str
) -> WorkspaceLayout:
    """已打开的账本反推布局：账本在根 = legacy，否则 run 根即账本父目录。

    借用账本时必须以**账本自己所在目录**为准——调用方可能已按 run 子目录
    打开它，若再按"根有没有账本"推断一次，会跟账本指向不同目录。这同时让
    ``run_flow(..., ledger=...)`` 的既有调用点在"工作区还没账本、稍后才由
    自己创建"的时序下保持零变化（账本就在根 → legacy）。
    """
    workspace = Path(workspace)
    ledger_directory = Path(ledger_path).resolve().parent
    return WorkspaceLayout(
        workspace_root=workspace,
        run_root=ledger_directory,
        run_name=run_name,
        legacy=ledger_directory == workspace,
    )


def layout_for_new_run(workspace: Path, run_name: str) -> WorkspaceLayout:
    """新 run 的 namespaced 布局：``<workspace>/<run_name>/``。

    T022 里内核的**写入路径**用这一个入口：它不依赖"现在根有没有账本"，
    因此"先建 run 目录、后落账本"的时序不会把第一次 run 误判成 legacy。
    """
    workspace_root = Path(workspace)
    return WorkspaceLayout(
        workspace_root=workspace_root,
        run_root=workspace_root / run_name,
        run_name=run_name,
        legacy=False,
    )