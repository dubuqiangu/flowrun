# FlowRun

Mermaid 流程图驱动的多 Agent 编排运行时:把 `flow.mmd` 编译为可执行 FlowIR,驱动 Claude Code / opencode 等 CLI Agent 按图执行——分支判定、人审、失败处置、预算闸、崩溃恢复全部落账可审计。

```
T001[实现登录API] ─┐
T002[实现登录UI]  ─┴→ J001[集成准备] → D001{验收通过?}
     ├─ when: ok     → H001([发布人审]) → T004[打 tag 发布]
     └─ when: fail   → T003[失败诊断]
     └─ timeout: 30m → T005[超时诊断]
```

- **纯标准库、Python ≥ 3.11**:零第三方依赖,src 布局,`pip install` 即用
- **真相只在账本**:每条事件带 sha256 前链绑定与幂等键,快照加速恢复(kill -9 后续跑与直跑语义等价)
- **闸与恢复闭环**:hop / 预算 / 非法边触发转人审,`flowrun resume` 或 todo `@resume` 恢复
- **失败可审计**:失败节点停在可审计位置,`out=` 产物逐项核对,退出码与语义信号双通道完成判定
- **注入安全**:agent 命令头是 Windows `.cmd/.bat` 垫片时自动切换为路径注入(packet 正文绝不进命令行,防 `cmd.exe` 重分词注入);垫片型 agent 用 `--file __FLOW_PACKET_PATH__` 显式取件

## 快速开始

```sh
pip install "git+https://github.com/dubuqiangu/flowrun.git"
# 私有仓库改用: pip install "git+ssh://git@github.com/dubuqiangu/flowrun.git"
```

写一个 `flow.mmd`(完整样例见 [examples/flow.mmd](examples/flow.mmd)):

```
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

然后:

```sh
flowrun compile flow.mmd                    # 编译校验,打印 FlowIR JSON
flowrun run flow.mmd --workspace ws         # 驱动到等待点或完成
flowrun status --workspace ws               # 节点状态 / 三闸计数 / 待人审
flowrun todo --workspace ws --flow flow.mmd # 重写 todo.md 投影(勾选回写)
```

退出码:`0` 完成 / `1` 等待(人审或暂停) / `2` 终止 / `3` 编译或校验错误 / `4` 工作区与流程不匹配 / `5` 工作区未初始化。

## 功能一览

| 功能 | 入口 | 说明 |
|---|---|---|
| 编译与校验 | `flowrun compile` | Mermaid + frontmatter → FlowIR;坏图带行号报错 |
| 流程执行 | `flowrun run` | fork/join、条件判定、人审、timeout/retry 边 |
| 闸与恢复 | `flowrun resume` / todo `@resume` | hop/预算/非法边触发转人审;人审豁免后继续 |
| 崩溃恢复 | 自动 | 事件账本 sha256 链 + 快照,断点续跑 |
| 人审裁决 | `flowrun approve/deny` / 勾选 todo | 落 `human_resolved`,幂等可审计 |
| CLI Agent 适配 | frontmatter `agents:` | 内置 claude-code/opencode,自定义 `cmd` 即插即用 |
| todo 投影 | `flowrun todo` | 从账本重建人审清单;`@pause`/`@skip`/`@resume` 标记回写 |
| 一键安装自验证 | `pwsh scripts/verify-install.ps1` | 推 GitHub → pip 直装 → 验证"装上的就是推的" |

## 文档

- [design.md](design.md) — 设计总览:五域架构、图语法、事件文法、闸与恢复语义
- [interfaces.md](interfaces.md) — 域间接口契约(冻结件)
- [tasks.md](tasks.md) — 任务清单与验收标准(M1 已收官)

## 开发

```sh
# 全量测试(纯标准库,src 布局零安装可跑;PowerShell 示例,其余 shell 换 PYTHONPATH=src)
$env:PYTHONPATH="src"; python -m unittest discover -s tests

# 一键安装自验证(在克隆的仓库内、发布后运行)
pwsh scripts/verify-install.ps1
```

## 许可

待定(M3-T027 定名查重与许可选择;当前倾向 Apache-2.0)。
