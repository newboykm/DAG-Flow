# Stop the dev servers started by start-dev.ps1 (kills listeners on 8000 / 5173)
$ErrorActionPreference = 'Continue'

Write-Host 'Stopping dev servers on ports 8000 / 5173 ...' -ForegroundColor Cyan
foreach ($port in @(8000, 5173)) {
    $listeners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        $ownerPid = $listener.OwningProcess
        Write-Host ("  stopping process {0} on port {1}" -f $ownerPid, $port)
        Stop-Process -Id $ownerPid -Force -ErrorAction SilentlyContinue
    }
}
Write-Host 'Done.' -ForegroundColor Green
