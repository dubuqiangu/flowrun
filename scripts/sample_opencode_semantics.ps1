# sample_opencode_semantics.ps1 — opencode 语义信号真机采样脚手架 (T020g)。
#
# 目标：采集 `opencode run --format json` 的真实终端输出样例，供 T020g 核定
#       idle / end 判定形态。本脚本只是【脚手架】：把最小 flow 跑进真实 opencode
#       CLI，捕获 stdout 到临时目录文件并打印路径，把"真机采样"这一步留给人来跑。
#
# 用法（参数/环境变量均可覆盖，脚本内不写死用户路径与 agent 名）：
#   pwsh scripts/sample_opencode_semantics.ps1
#   # 或显式指定
#   pwsh scripts/sample_opencode_semantics.ps1 -Workspace D:\tmp\ws -Message "print hello"
#
# 注意：
#   - 需要真实 opencode 可执行文件在 PATH 上。
#   - opencode 语义信号会随版本变化，本脚本把每次采样放进带时间戳的文件，便于对比。
#   - 若当前环境无法连通 opencode / 无真实终端，脚本会在捕获前失败——没关系，
#     这是外部手动步骤；脚本只要语法可运行、逻辑正确即可。
#   - 人工跑通后，请把样本内容粘进 tasks.md 的 T020g 验收证据。

param(
  # opencode 可执行文件名；不写死绝对路径，只认 PATH 上的命令。
  [string]$OpenCode = "opencode",
  # 最小 flow 的工作区目录；留空则自建临时目录。
  [string]$Workspace = "",
  # 采样 agent（一个 提示词）——留空则用默认 agent。
  [string]$Message = "输出一行文本并结束（只做冒烟，不触发耗时动作）",
  # 输出目录；留空则用系统临时目录下的 sample_opencode_semantics。
  [string]$OutDir = "",
  # 可选：指定 opencode agent 名（如 build / general）；不写死，留空用默认。
  [string]$AgentName = ""
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

# --- 定位 repo 根（本脚本位于 scripts/ 下）------------------------------------------
$repoRoot = Split-Path $PSScriptRoot -Parent

# --- 输出目录：默认系统临时目录下带时间戳 -------------------------------------------
if (-not $OutDir) {
  $OutDir = Join-Path ([System.IO.Path]::GetTempPath()) "sample_opencode_semantics"
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$captureFile = Join-Path $OutDir ("opencode-sample-$stamp.json")
$workspaceDir = $Workspace
if (-not $workspaceDir) {
  $workspaceDir = Join-Path $OutDir ("ws-$stamp")
  New-Item -ItemType Directory -Force -Path $workspaceDir | Out-Null
}

Write-Output "== opencode 语义信号采样 =="
Write-Output "workspace : $workspaceDir"
Write-Output "capture   : $captureFile"

# --- 确认 opencode 可执行 -----------------------------------------------------------
$ocCmd = Get-Command $OpenCode -ErrorAction SilentlyContinue
if (-not $ocCmd) {
  Write-Error "找不到 opencode 可执行: '$OpenCode'（请确保其在 PATH 上，或传 -OpenCode）。真机采样无法在此环境自动完成，请在真实 opencode 终端手动执行。"
}

# --- 构造 opencode 调用 -------------------------------------------------------------
# 目标命令（待真机核对参数）：
#   opencode run --format json --agent <agent> "<message>" <workspace>
# 说明：
#   * `--format json` 是 `opencode run` 子命令的合法 flag（choices: default|json）。
#   * 工作区目录按 base 用法 `opencode <subcommand> [flags] [<directory>]` 作为位置参数传入；
#     不同 opencode 版本对"目录 vs message"的位置解析可能不同，真机跑之前请先核对。
#   * `--agent` 为可选；不写死 agent 名，仅当用户显式传 -AgentName 时才追加。
#   * T020g 需要采集的是"端到端 idle/end 语义信号"，建议真机跑时把提示词设计成
#     会产出 JSON 事件流的最小任务，并把 stdout 原样存档。
if ($AgentName) {
  $jsonArgs = @($OpenCode, "run", "--format", "json", "--agent", $AgentName, "--message", $Message, $workspaceDir)
} else {
  $jsonArgs = @($OpenCode, "run", "--format", "json", "--message", $Message, $workspaceDir)
}
$captureContent = "## 采样时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')`n## 命令: $($jsonArgs -join ' ')`n"
$captureContent | Out-File -FilePath $captureFile -Encoding utf8

# --- 执行并把 stdout 追加入捕获文件 -------------------------------------------------
# 注意：真机采样中 opencode 可能交互等待/需要 TTY；这里只做捕获骨架，跑不通就报错留痕。
Write-Output "执行: $($jsonArgs -join ' ')"
try {
  $ocArgs = $jsonArgs[1..($jsonArgs.Count - 1)]
  & $OpenCode @ocArgs 2>&1 |
    Add-Content -Path $captureFile -Encoding utf8
} catch {
  $_.Exception.Message | Add-Content -Path $captureFile -Encoding utf8
  Write-Warning "opencode 执行失败（见捕获文件），可能在无 TTY/无真实终端环境无法完成——请手动真机采样。"
}

Write-Output "捕获完成: $captureFile"
Write-Output "---"
Write-Output "人工跑通后，请把样本粘进 tasks.md 的 T020g 验收证据（核定 idle/end 判定形态）。"