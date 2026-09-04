# Ручной полный прогон: расширенный поиск каналов + весь цикл с отправкой.
# Сессия Telethon монопольна, поэтому демон останавливается на время прогона
# и ПОДНИМАЕТСЯ ОБРАТНО в конце — что бы ни случилось посередине (finally).
$ErrorActionPreference = "Continue"
Set-Location "C:\Users\User\Desktop\jobhunter"
$log = "out\logs\manual_run_$(Get-Date -Format 'yyyyMMdd_HHmm').log"
function Log($m) { "$(Get-Date -Format 'HH:mm:ss') $m" | Tee-Object -FilePath $log -Append }

try {
    Log "=== стоп демона ==="
    docker compose stop autopilot 2>&1 | Tee-Object -FilePath $log -Append

    # Убедиться, что демон РЕАЛЬНО остановлен: два Telethon-клиента на одном
    # файле сессии — это AuthKeyDuplicatedError и разлогин аккаунта.
    $state = docker inspect -f '{{.State.Running}}' jobhunter-autopilot-1 2>$null
    if ($state -eq 'true') {
        Log "ОТМЕНА: контейнер autopilot всё ещё работает — прогона не будет."
        exit 1
    }

    Log "=== автопоиск каналов (лимит 90, удалёнка-запросы) ==="
    docker compose run --rm autopilot python -m jobhunter.ingest.discover --apply --limit 90 2>&1 |
        Tee-Object -FilePath $log -Append

    Log "=== полный цикл: сбор -> подготовка -> одобрение -> отправка ==="
    docker compose run --rm autopilot python -m jobhunter.autopilot --once 2>&1 |
        Tee-Object -FilePath $log -Append
}
finally {
    Log "=== подъём демона ==="
    docker compose up -d autopilot 2>&1 | Tee-Object -FilePath $log -Append
    Log "=== готово ==="
}
