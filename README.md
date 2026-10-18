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
- **真相只在账本**:每条事件带 sha256 前链绑定与幂等键,快照加速恢复(kill -9 后续跑与直跑语义等价);恢复路径增量化:链校验只校验新增条目(文件变短时回退全量),最新快照记录走进程内缓存——磁盘格式与恢复等价性不变
- **闸与恢复闭环**:hop / 预算 / 非法边触发转人审,`flowrun resume` 或 todo 行尾 HTML 注释 `<!-- @resume -->` 恢复;hop 与 budget **各自独立判定**,同一轮可同时命中并各落 `gate_tripped`,一个闸被豁免不连坐另一个闸(按闸名分别"至多一次 + 其后有 `flow_resumed` 即永久豁免")
- **工作区身份**:以账本 `flow_started` 为身份真相源,其 `planned_nodes` 与当前图节点集**严格集合相等**(多一个或少一个都拒,"换图请换工作区"),不符退 `4`;校验**零写入**且在一切写入之前执行,`run` 与 `resume` 同一套
- **失败可审计**:失败节点停在可审计位置,`out=` 产物逐项核对,退出码与语义信号双通道完成判定
- **注入安全**:agent 命令头是 Windows `.cmd/.bat/.ps1` 垫片时自动切换为路径注入(packet 正文绝不进命令行,防 `cmd.exe` 重分词注入);`.cmd/.bat` 经 `subprocess.list2cmdline` 生成整条命令行交 cmd.exe,`.ps1` 经 `powershell -NoProfile -ExecutionPolicy Bypass -File` 启动;垫片型 agent 用 `--file __FLOW_PACKET_PATH__` 显式取件

## 快速开始

```sh
pip install "git+https://github.com/dubuqiangu/flowrun.git"
# 需要写权限(提 PR / 私有 fork)时改用 SSH: pip install "git+ssh://git@github.com/dubuqiangu/flowrun.git"
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

退出码:`0` 完成 / `1` 等待(人审或暂停) / `2` 终止(M1 保留位,无生产者,见 tasks.md 台账) / `3` 编译或校验错误 / `4` 工作区身份不符 / `5` 工作区未初始化 / `6` 账本损坏(中段不可解析 / 哈希链断裂 / 快照核对失败 / 快照记录形状非法)。退出码 `6` 由 CLI 入口统一捕获,stderr 输出含 `ledger.jsonl` 路径与原因的可读诊断,**不打 Python traceback**。

## 功能一览

| 功能 | 入口 | 说明 |
|---|---|---|
| 编译与校验 | `flowrun compile` | Mermaid + frontmatter → FlowIR;坏图带行号报错 |
| 流程执行 | `flowrun run` | fork/join、条件判定、人审、timeout/retry 边 |
| 闸与恢复 | `flowrun resume` / todo `<!-- @resume -->` | hop/预算/非法边触发转人审;hop 与 budget 独立判定、各自落 `gate_tripped`,每闸名至多一次,人审豁免后继续且不连坐 |
| 崩溃恢复 | 自动 | 事件账本 sha256 链 + 快照,断点续跑;链校验与快照加载均走增量 |
| 人审裁决 | `flowrun approve/deny` / 勾选 todo | 落 `human_resolved`,幂等可审计 |
| CLI Agent 适配 | frontmatter `agents:` | 内置 claude-code/opencode,自定义 `cmd` 即插即用 |
| todo 投影 | `flowrun todo` | 从账本重建人审清单;指令标记见下(仅行尾 HTML 注释形式) |
| 一键安装自验证 | `pwsh scripts/verify-install.ps1` | 推 GitHub → pip 直装 → 验证"装上的就是推的" |

## todo 指令标记

指令只在**节点行行尾的 HTML 注释**里生效(注释内外空白随意):

```
- [ ] T002 生成报告 <!-- @pause -->
- [ ] T005 清理缓存 <!-- @skip -->
- [ ] T007 发布 <!-- @resume -->
```

- `@pause` 挂起此处;`@skip` 跳过该节点(该行保留但不勾选);`@resume` 解除暂停。
- `@resume` 是**全局**标记:仅当流确为暂停态才落 `flow_resumed{by:"todo"}`,否则告警忽略。
- 节点 label 文本里的裸 `@pause`/`@skip`/`@resume` **永不触发**——label 是数据,不是指令;旧式裸标记已失效,解析到节点行上的裸标记时每轮每文件至多给一条升级告警。
- 生成的头/横幅注释**不得出现嵌套 HTML 注释**(Markdown 会提前闭合)。

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

MIT License，全文见 [LICENSE](LICENSE)。
