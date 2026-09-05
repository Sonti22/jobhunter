# Consistent SQLite snapshots. ASCII source works in Windows PowerShell 5.1.
# Does not stop services, open Telegram, modify the source volume or delete backups.
[CmdletBinding()]
param(
    [string]$Image = "jobhunter:latest",
    [string]$Volume = "jobhunter-data"
)
$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$backupDir = Join-Path $projectRoot "backup"
$backupTool = (Resolve-Path -LiteralPath (Join-Path $projectRoot "jobhunter\backup.py")).Path
$archiveName = "jobhunter-data-" + (Get-Date -Format "yyyyMMdd_HHmmss") + "-" + [guid]::NewGuid().ToString("N").Substring(0,8) + ".tgz"

# Never create an empty Docker volume because of a misspelled source name.
docker volume inspect $Volume --format '{{.Name}}' | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Source Docker volume does not exist: $Volume" }
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
$dockerArgs = @(
    "run", "--rm", "--network", "none", "--entrypoint", "python",
    "--mount", "type=volume,src=$Volume,dst=/data,readonly",
    "--mount", "type=bind,src=$backupDir,dst=/backup",
    "--mount", "type=bind,src=$backupTool,dst=/backup_tool.py,readonly",
    $Image, "/backup_tool.py", "create", "--source", "/data",
    "--output", "/backup/$archiveName"
)
docker @dockerArgs
if ($LASTEXITCODE -ne 0) { throw "Backup failed. Existing backups were kept." }
Write-Host "Verified backup created: $(Join-Path $backupDir $archiveName)"
Write-Host "Existing backups were kept. Archives contain credentials; do not publish them."
