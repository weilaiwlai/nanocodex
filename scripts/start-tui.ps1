# nanocodex TUI 启动脚本 (Windows)
# 解决中文乱码问题：先切换控制台为 UTF-8 编码

$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'

# 切换 Windows 控制台代码页到 UTF-8
chcp 65001 | Out-Null

Write-Host "正在启动 nanocodex TUI..." -ForegroundColor Cyan

# 检测 conda 环境自动设置 Python 路径
$condaPython = "C:\Users\Administrator\anaconda3\envs\nanocodex\python.exe"
if (Test-Path $condaPython) {
    $env:NANOCODOX_TUI_PYTHON = $condaPython
}

Set-Location (Split-Path -Parent $PSScriptRoot)
npm --prefix tui run dev
