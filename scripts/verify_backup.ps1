# Verifies the archive in an isolated container; never mounts the working volume.
[CmdletBinding()]
param(
    [string]$Archive = "",
    [string]$Image = "jobhunter:latest"
)
$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$backupTool = (Resolve-Path -LiteralPath (Join-Path $projectRoot "jobhunter\backup.py")).Path
if ($Archive) {
    $archiveFile = Get-Item -LiteralPath $Archive
} else {
    $archiveFile = Get-ChildItem -LiteralPath (Join-Path $projectRoot "backup") -File -Filter "jobhunter-data-*.tgz" |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
}
if (-not $archiveFile -or $archiveFile.PSIsContainer) { throw "No backup archive found." }
$dockerArgs = @(
    "run", "--rm", "--network", "none", "--entrypoint", "python",
    "--mount", "type=bind,src=$($archiveFile.FullName),dst=/archive.tgz,readonly",
    "--mount", "type=bind,src=$backupTool,dst=/backup_tool.py,readonly",
    $Image, "/backup_tool.py", "verify", "--archive", "/archive.tgz"
)
docker @dockerArgs
if ($LASTEXITCODE -ne 0) { throw "Archive verification failed: $($archiveFile.FullName)" }
Write-Host "Verified backup: $($archiveFile.FullName)"
