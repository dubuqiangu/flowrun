# verify-install.ps1 — FlowRun 一键安装自验证(发布门禁)。
# 任意位置运行:  pwsh <repo>/scripts/verify-install.ps1
#
# 链条: Python >= 3.11 → git 仓库就绪(本地 HEAD 已推送 origin、工作区干净)
#       → 临时 venv 从 GitHub 直装(钉到本地 HEAD 的 SHA) → 安装版本号与 pyproject 一致
#       → 磁盘安装文件树与 git ls-files 镜像一致 → 安装副本 console script compile 冒烟
#       → 安装副本跑全量 unittest → 零 FAIL 才退出码 0。
# 失败时保留临时 venv 供诊断;成功后清理(加 -KeepTemp 始终保留)。
param(
  [string]$InstallUrl = "git+ssh://git@github.com/dubuqiangu/flowrun.git",
  [string]$ExpectedVersion = "",
  [switch]$KeepTemp
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$repoRoot = Split-Path $PSScriptRoot -Parent
$script:failures = 0
$script:tempVenv = $null
$script:smokeRoot = $null
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"

function Assert-Check {
  param([string]$Label, [bool]$Condition, [string]$Detail = "")
  $suffix = if ($Detail) { "  ($Detail)" } else { "" }
  if ($Condition) {
    Write-Output ("PASS  " + $Label + $suffix)
  } else {
    Write-Output ("FAIL  " + $Label + $suffix)
    $script:failures++
  }
}

# --- 1. Python 解释器与版本 ------------------------------------------------------------
$pythonVersion = $null
try {
  $pythonVersion = (& python -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null | Select-Object -First 1)
} catch { $pythonVersion = $null }
Assert-Check "python on PATH" ($null -ne $pythonVersion) "reported=$pythonVersion"

$versionOk = $false
if ($null -ne $pythonVersion) {
  & python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
  $versionOk = ($LASTEXITCODE -eq 0)
}
Assert-Check "Python >= 3.11" $versionOk "required for PEP 604 union syntax"

# --- 2. 预期版本号(读 pyproject,单一事实源) -----------------------------------------
$expectedVersionValue = $ExpectedVersion
if (-not $expectedVersionValue) {
  $pyprojectPath = Join-Path $repoRoot "pyproject.toml"
  if (Test-Path $pyprojectPath) {
    $pyprojectText = Get-Content $pyprojectPath -Raw
    if ($pyprojectText -match '(?m)^version\s*=\s*"([^"]+)"') { $expectedVersionValue = $Matches[1] }
  }
}
Assert-Check "pyproject version readable" ($null -ne $expectedVersionValue -and $expectedVersionValue -ne "") "expected=$expectedVersionValue"

# --- 3. git 仓库就绪:HEAD 已推送、工作区干净 -----------------------------------------
Set-Location $repoRoot
$isGitRepo = Test-Path (Join-Path $repoRoot ".git")
Assert-Check "git repository initialized" $isGitRepo

$localHead = $null
$remoteHead = $null
$branchName = $null
$dirtyFiles = @()
$trackedCount = 0
if ($isGitRepo) {
  $localHead = (git rev-parse HEAD 2>$null | Select-Object -First 1)
  $branchName = (git rev-parse --abbrev-ref HEAD 2>$null | Select-Object -First 1)
  $remoteHeads = @(git ls-remote origin "refs/heads/$branchName" 2>$null)
  if ($remoteHeads.Count -gt 0) { $remoteHead = ($remoteHeads[0] -split "\s+")[0] }
  try { $dirtyFiles = @(git status --porcelain) } catch { $dirtyFiles = @("<git status failed>") }
  $trackedCount = @(git ls-files).Count
}
$branchLabel = if ($branchName) { $branchName } else { "main" }
Assert-Check "local HEAD pushed to origin/$branchLabel" (
  $null -ne $localHead -and $localHead -eq $remoteHead
) "head=$localHead remote=$remoteHead"

Assert-Check "working tree fully committed (nothing untracked/modified)" (
  $isGitRepo -and $dirtyFiles.Count -eq 0
) "tracked=$trackedCount uncommitted=$($dirtyFiles.Count)"

# --- 4. 临时 venv 从 GitHub 直装(钉本 HEAD SHA) -------------------------------------
$venvPython = $null
$installOk = $false
if ($null -ne $localHead) {
  $script:tempVenv = Join-Path ([System.IO.Path]::GetTempPath()) "flowrun-verify-$timestamp"
  & python -m venv $script:tempVenv 2>&1 | Out-Null
  $venvCreated = ($LASTEXITCODE -eq 0)
  if ($venvCreated) {
    $venvPython = Join-Path $script:tempVenv "Scripts\python.exe"
    if (-not (Test-Path $venvPython)) { $venvPython = Join-Path $script:tempVenv "bin/python" }
    if (-not (Test-Path $venvPython)) { $venvCreated = $false; $venvPython = $null }
  }
  Assert-Check "temp venv created" $venvCreated "path=$($script:tempVenv)"

  if ($venvCreated) {
    $installSpec = "$InstallUrl@$localHead"
    $installOutput = (& $venvPython -m pip install --disable-pip-version-check --no-input $installSpec 2>&1 | Out-String)
    $installOk = ($LASTEXITCODE -eq 0)
    Assert-Check "pip install from GitHub at HEAD ($($localHead.Substring(0, 12)))" $installOk "spec=$installSpec"
    if (-not $installOk) {
      Write-Output ($installOutput -split "`n" | Select-Object -Last 15 | ForEach-Object { "      " + $_ })
    }
  }
} else {
  Assert-Check "pip install from GitHub at HEAD" $false "skipped: local HEAD unavailable"
}

# --- 5. 安装版本号与磁盘文件树镜像 ----------------------------------------------------
if ($installOk) {
  $installedVersion = (& $venvPython -c "import importlib.metadata as metadata; print(metadata.version('flowrun'))" 2>$null | Select-Object -First 1)
  Assert-Check "installed version matches pyproject" ($installedVersion -eq $expectedVersionValue) "installed=$installedVersion expected=$expectedVersionValue"

  $listCode = 'import flowrun,pathlib;root=pathlib.Path(flowrun.__file__).parent;print(chr(10).join(sorted(str(p.relative_to(root)).replace(chr(92),"/") for p in root.rglob("*.py"))))'
  $installedModuleFiles = @((& $venvPython -c $listCode 2>$null) -split "`n" | Where-Object { $_ -and $_.Trim() -ne "" } | ForEach-Object { $_.Trim() })
  $repoModuleFiles = @(git ls-files "src/flowrun" | Where-Object { $_ -like "*.py" } | ForEach-Object { $_ -replace "^src/", "" })
  $missingInInstall = @($repoModuleFiles | Where-Object { $installedModuleFiles -notcontains $_ })
  $extraInInstall = @($installedModuleFiles | Where-Object { $repoModuleFiles -notcontains $_ })
  Assert-Check "installed package mirrors repo src/flowrun" (
    $missingInInstall.Count -eq 0 -and $extraInInstall.Count -eq 0
  ) "repo=$($repoModuleFiles.Count) installed=$($installedModuleFiles.Count) missing=$($missingInInstall.Count) extra=$($extraInInstall.Count)"
  if ($missingInInstall.Count -gt 0) { Write-Output ("      missing: " + ($missingInInstall -join ", ")) }
  if ($extraInInstall.Count -gt 0) { Write-Output ("      extra:   " + ($extraInInstall -join ", ")) }
} else {
  Assert-Check "installed version matches pyproject" $false "skipped: install failed"
  Assert-Check "installed package mirrors repo src/flowrun" $false "skipped: install failed"
}

# --- 6. 安装副本 console script compile 冒烟 ------------------------------------------
$compileJson = $null
if ($installOk) {
  $flowrunExe = Join-Path $script:tempVenv "Scripts\flowrun.exe"
  if (-not (Test-Path $flowrunExe)) { $flowrunExe = Join-Path $script:tempVenv "bin/flowrun" }
  Assert-Check "console script 'flowrun' installed" (Test-Path $flowrunExe) "path=$flowrunExe"

  if (Test-Path $flowrunExe) {
    $script:smokeRoot = Join-Path ([System.IO.Path]::GetTempPath()) "flowrun-smoke-$timestamp"
    New-Item -ItemType Directory -Path $script:smokeRoot -Force | Out-Null
    $smokeFlowPath = Join-Path $script:smokeRoot "flow.mmd"
    @'
---
run: verify-smoke
defaults: {budget_tokens: 100000}
agents:
  verify-agent: {cmd: "python -c pass"}
---
flowchart LR
    T001[冒烟甲;;agent=verify-agent] --> T002[冒烟乙;;agent=verify-agent]
'@ | Set-Content -Path $smokeFlowPath -Encoding UTF8
    try {
      $compileRaw = (& $flowrunExe compile $smokeFlowPath 2>$null | Out-String)
      $compileJson = $compileRaw | ConvertFrom-Json
    } catch { $compileJson = $null }
    $compileOk = ($null -ne $compileJson) -and ($compileJson.run_name -eq "verify-smoke") -and
      (@($compileJson.entry_node_ids) -contains "T001")
    Assert-Check "installed CLI compiles smoke flow to valid FlowIR" $compileOk "run=$($compileJson.run_name) entry=$(@($compileJson.entry_node_ids) -join ',')"
  } else {
    Assert-Check "installed CLI compiles smoke flow to valid FlowIR" $false "skipped: console script missing"
  }
} else {
  Assert-Check "console script 'flowrun' installed" $false "skipped: install failed"
  Assert-Check "installed CLI compiles smoke flow to valid FlowIR" $false "skipped: install failed"
}

# --- 7. 安装副本跑全量 unittest(无 PYTHONPATH,import 必解析到安装包) -----------------
if ($installOk) {
  $savedPythonPath = $env:PYTHONPATH
  Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
  Push-Location $repoRoot
  $testOutput = (& $venvPython -m unittest discover -s tests 2>&1 | Out-String)
  $testExitCode = $LASTEXITCODE
  Pop-Location
  if ($null -ne $savedPythonPath) { $env:PYTHONPATH = $savedPythonPath }

  $testSummaryLine = ($testOutput -split "`n" | Where-Object { $_ -match "^Ran \d+ tests" } | Select-Object -First 1)
  $testCount = if ($testSummaryLine -match "Ran (\d+) tests") { [int]$Matches[1] } else { 0 }
  Assert-Check "installed copy: unittest suite green" (
    $testExitCode -eq 0 -and $testOutput -match "OK" -and $testCount -ge 200
  ) "tests=$testCount (expect >= 200)"
  if ($testExitCode -ne 0) {
    Write-Output ($testOutput -split "`n" | Where-Object { $_ -match "^(FAIL|ERROR):" } | ForEach-Object { "      " + $_ })
  }
} else {
  Assert-Check "installed copy: unittest suite green" $false "skipped: install failed"
}

# --- 收尾 -------------------------------------------------------------------------------
if ($null -ne $script:smokeRoot -and (Test-Path $script:smokeRoot) -and $script:failures -eq 0) {
  Remove-Item $script:smokeRoot -Recurse -Force -ErrorAction SilentlyContinue
}
if ($null -ne $script:tempVenv) {
  if ($script:failures -eq 0 -and -not $KeepTemp) {
    Remove-Item $script:tempVenv -Recurse -Force -ErrorAction SilentlyContinue
  } else {
    Write-Output "temp venv kept for diagnosis: $($script:tempVenv)"
  }
}

Write-Output "----"
if ($script:failures -eq 0) {
  Write-Output "VERIFY OK — GitHub 直装已闭环: 安装版本 = pyproject、文件树 = 仓库镜像、CLI 冒烟通过、安装副本全量测试绿"
  exit 0
}
Write-Output ("VERIFY FAILED — $($script:failures) check(s) failed")
exit 1
