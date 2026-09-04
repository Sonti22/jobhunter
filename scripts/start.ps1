# Запуск jobhunter в Docker.
#
# Страховка к автозапуску Docker Desktop: `restart: unless-stopped` поднимает
# контейнеры сам, но только если проект не был остановлен вручную через
# `docker compose down`. Этот скрипт поднимает в любом случае.
#
# Положить ярлык в автозагрузку:
#   1. Win+R → shell:startup
#   2. Создать ярлык на:
#      powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\Users\User\Desktop\jobhunter\scripts\start.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Docker Desktop после входа в систему стартует не мгновенно: ждём демон,
# иначе compose падает на «cannot connect to the Docker daemon».
$deadline = (Get-Date).AddMinutes(5)
while ((Get-Date) -lt $deadline) {
    docker info 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 10
}

if ($LASTEXITCODE -ne 0) {
    Write-Error "Docker Desktop не поднялся за 5 минут — запусти его вручную"
    exit 1
}

docker compose up -d
docker compose ps
