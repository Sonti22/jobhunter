# Резервная копия тома jobhunter-data в архив на диске.
#
# Том живёт внутри виртуальной машины WSL2 — его не видно в Проводнике и он
# исчезнет вместе с `docker volume rm` или переустановкой Docker Desktop.
# Архив лежит на обычном диске и переживает и то, и другое.
#
# Разовый запуск:
#   powershell -ExecutionPolicy Bypass -File scripts\backup_volume.ps1
#
# Раз в неделю (создать задание в Планировщике):
#   schtasks /create /tn "jobhunter backup" /sc weekly /d SUN /st 04:00 ^
#     /tr "powershell -ExecutionPolicy Bypass -File C:\Users\User\Desktop\jobhunter\scripts\backup_volume.ps1"

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$dir  = Join-Path $root "backup"
$stamp = Get-Date -Format "yyyyMMdd_HHmm"

New-Item -ItemType Directory -Force -Path $dir | Out-Null

# Сводим WAL перед снимком: копия базы без журнала теряет последние
# транзакции, а копия с несогласованным журналом хуже, чем без него.
docker compose -f (Join-Path $root "docker-compose.yml") exec -T autopilot `
    python -c "import sqlite3,os; c=sqlite3.connect(os.environ.get('DB_PATH','/data/jobhunter.db'),timeout=60); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()" 2>$null

docker run --rm -v jobhunter-data:/data -v "${dir}:/backup" `
    alpine tar czf "/backup/jobhunter-data-$stamp.tgz" -C /data .

$file = Join-Path $dir "jobhunter-data-$stamp.tgz"
if (Test-Path $file) {
    $size = [math]::Round((Get-Item $file).Length / 1MB, 1)
    # Keep runtime output ASCII: Windows PowerShell 5.1 may parse a UTF-8
    # script without BOM using the active code page and corrupt quoted text.
    Write-Host "Backup created: $file ($size MB)"
} else {
    Write-Error "Backup archive was not created"
}

# Держим последние 8 копий: две недели истории при еженедельном запуске.
Get-ChildItem $dir -Filter "jobhunter-data-*.tgz" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 8 |
    ForEach-Object { Remove-Item $_.FullName; Write-Host "Removed old backup: $($_.Name)" }
