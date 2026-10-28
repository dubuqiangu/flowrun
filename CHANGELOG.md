# Changelog

FlowRun 迭代记录。版本号遵循 pyproject.toml 的 `version`（当前 `0.1.0`）。

## 0.1.0（进行中）

本仓库自 2026-10-16 起持续迭代，核心里程碑按提交顺序：

### T022 多 run 隔离 + T027 发行名定稿
- 工作区根无 `ledger.jsonl` → 每个 run 独占 `<workspace>/<run>/`（账本/快照/packets/audit/todo 各一份，agent cwd = run 根，双 run 互不串扰）。
- 根有 `ledger.jsonl` → legacy 扁平布局零迁移，行为逐字节不变。
- 发行名定为 `flowrun-mmd`（PyPI 的 `flowrun` 被他人占用；`flowrun-mmd` 在 USPTO/EUIPO、PyPI、npm、crates.io、GitHub 全部 clear）。仓库名与导入名仍为 `flowrun`。

### T020e 失败处置定稿：独立闸名 `retry_exhausted`
- 失败/重试耗尽转人审从含糊的 `gate_tripped(gate="retry")` 改为 `gate_tripped(gate="retry_exhausted")`，与机器闸（hop/budget/verdict/illegal_edge）区分。
- 保留 `GATE_RETRY`（废弃待删，兼容旧账本）；沿用"每工作区每闸名至多一次 + resume 豁免"幂等语义。
- 16 处测试断言逐场景核对更新；529 tests OK。

### T018 retry/timeout 边语义
- `retry: N` 耗尽转人审；`timeout: 30m` 到点走指定边；重试计数跨进程经 `node_entered` 的 `failure_count` 恢复。

### T020a 判定 usage 入账
- 判定消耗与 task 路径同口径落 `agent_exited{payload.tokens}`，计入 `token_spent()` 与 budget 闸。

### T020b 审计产物归位
- agent stdout/stderr 落 run 根 `audit/`，命名 `<node_id>-<kind>-<attempt>.stdout.log`，`attempt` 由账本重建。

### T020c apply_user_marks 降噪
- done 派生的 `[x]` 行不再每轮重复告警。

### T020d EventLedger 单实例化
- `run_flow(..., ledger=None)` 借用语义；cli 整轮只开一个账本。

### 注入安全
- Windows `.cmd/.bat/.ps1` 垫片自动切换路径注入（packet 正文不进命令行）；`.cmd/.bat` 逐参数加引号（含 `%`/`"`/换行参数直接拒绝）；`.ps1` 经 `-ExecutionPolicy Bypass -File` 启动。

### M1 收官 + 审计修复
- 三线并行的 M1 收官批次 + 全项目审计修复（P1 完成度、P2 七项、usage 重复计数、只读账本视图、引擎句柄泄漏、编译/校验层与控制流缺陷、适配层注入面加固、内核身份/边身份判定异常）。
- 结构拆分：判定节点执行器、状态概览、CLI 等按域解耦（纯搬移、行为零变化）。

## 未发布说明

- 当前仅从 GitHub 直装；PyPI 发布后用 `pip install flowrun-mmd`。
- 完整逐项任务与验收标准见 [tasks.md](tasks.md)。