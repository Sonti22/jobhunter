# Проверка последней резервной копии jobhunter-data БЕЗ восстановления поверх БД.
#
# «tar tzf» проверял только читаемость оглавления: пустой архив или архив без
# базы проходил как целый. Настоящая проверка — извлечь jobhunter.db во
# временный каталог контейнера и прогнать PRAGMA integrity_check.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$dir = Join-Path $root "backup"
$file = Get-ChildItem -LiteralPath $dir -Filter "jobhunter-data-*.tgz" |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $file) {
    Write-Error "В $dir нет архивов jobhunter-data-*.tgz"
    exit 1
}

# Образ проекта: в нём есть python с sqlite3 — ничего не докачиваем.
docker run --rm -v "$($file.FullName):/backup/archive.tgz:ro" `
    --entrypoint sh jobhunter:latest -c @'
set -e
mkdir -p /tmp/verify
tar xzf /backup/archive.tgz -C /tmp/verify
db=$(find /tmp/verify -name "jobhunter.db" | head -1)
if [ -z "$db" ]; then
    echo "В архиве нет jobhunter.db" >&2
    exit 3
fi
python - "$db" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
row = conn.execute("PRAGMA integrity_check").fetchone()
apps = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
if row[0] != "ok":
    print("integrity_check:", row[0], file=sys.stderr)
    sys.exit(4)
if apps == 0:
    print("База пуста: 0 заявок — бэкап подозрителен", file=sys.stderr)
    sys.exit(5)
print("integrity ok, заявок в бэкапе:", apps)
PY
'@
if ($LASTEXITCODE -ne 0) {
    Write-Error "Бэкап НЕ восстановим: $($file.FullName) (код $LASTEXITCODE)"
    exit 2
}
Write-Host "Бэкап восстановим: $($file.FullName)"
