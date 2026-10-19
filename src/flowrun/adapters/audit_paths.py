"""审计产物命名/路径族：审计目录名、产物类别、文件名与 attempt 重建。

写侧（适配器 / hooks_assembly）与读侧（engine_evidence）共用同一套命名真相，
本模块是它的唯一归属；不依赖 ``flowrun.adapters.base``（避免环）。

``flowrun.adapters.base`` 会把这里的公开名字原样重新导出，因此
``from flowrun.adapters.base import AuditArtifact, audit_stdout_file_name, ...``
的历史导入路径保持可用。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# 审计产物落盘的子目录名（相对工作区根）：审计永不落工作区根。
AUDIT_DIRECTORY_NAME = "audit"
AUDIT_STDOUT_SUFFIX = ".stdout.log"
AUDIT_STDERR_SUFFIX = ".stderr.log"

# 审计产物类别（命名第二段，读侧据此与写侧对齐）。
AUDIT_KIND_AGENT = "agent"            # 任务/判定 agent 的正常投递
AUDIT_KIND_JUDGE = "judge"            # 判定节点 agent 的投递（decision 节点）
AUDIT_KIND_NO_ADAPTER = "no-adapter"  # 防御分支：probe 通过却无可用适配器


def audit_artifact_stem(node_id: str, kind: str, attempt: int) -> str:
    """审计产物基名：``<node_id>-<kind>-<attempt>``（写侧/读侧共用的唯一命名真相）。"""
    return f"{node_id}-{kind}-{attempt}"


def audit_stdout_file_name(node_id: str, kind: str, attempt: int) -> str:
    """审计 stdout 文件名：``<node_id>-<kind>-<attempt>.stdout.log``。"""
    return f"{audit_artifact_stem(node_id, kind, attempt)}{AUDIT_STDOUT_SUFFIX}"


def audit_stderr_file_name(node_id: str, kind: str, attempt: int) -> str:
    """审计 stderr 文件名（与 stdout 同名配对）：``<node_id>-<kind>-<attempt>.stderr.log``。"""
    return f"{audit_artifact_stem(node_id, kind, attempt)}{AUDIT_STDERR_SUFFIX}"


def audit_stdout_relative_path(node_id: str, kind: str, attempt: int) -> Path:
    """审计 stdout 相对工作区根的路径（``audit/<文件名>``）——读侧据此定位文件。

    返回 ``Path`` 时用正斜杠稳定形态（``as_posix`` 供落进账本/证据的字符串），
    与 ``Path(work_dir) / ...`` 拼接在 Windows / POSIX 上都能解析。
    """
    return Path(AUDIT_DIRECTORY_NAME) / audit_stdout_file_name(node_id, kind, attempt)


def dispatch_attempt_from_ledger_entries(ledger_entries, node_id: str) -> int:
    """该节点最近一次投递的 attempt 号——**仅凭账本信息重建**（不解析幂等键）。

    执行器每次投递前先落一条 ``agent_dispatched``，故"该节点的 agent_dispatched
    条数"恰等于最近一次投递的 attempt；写侧命名与读侧定位都以此为准，跨进程/恢复
    后可精确重建，不依赖任何进程内状态。
    """
    return sum(
        1
        for entry in ledger_entries
        if entry.type == "agent_dispatched" and entry.node == node_id
    )


@dataclass(frozen=True)
class AuditArtifact:
    """一次派发审计产物的身份：目录 + 节点 + 类别 + attempt。

    写侧（适配器 / 各 caller）据此把 stdout/stderr 落到审计目录下的规范名；
    同 attempt 重投会打开同名文件（``"wb"`` 截断）→ 覆盖，不留第二种命名。
    """

    directory: Path
    node_id: str
    kind: str
    attempt: int

    def stdout_path(self) -> Path:
        return Path(self.directory) / audit_stdout_file_name(
            self.node_id, self.kind, self.attempt
        )

    def stderr_path(self) -> Path:
        return Path(self.directory) / audit_stderr_file_name(
            self.node_id, self.kind, self.attempt
        )