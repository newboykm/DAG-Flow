# One-click dev launcher: backend (uvicorn:8000) + frontend (vite:5173)
# Double-click start-dev.cmd, or run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File start-dev.ps1
# Opens two separate console windows (one per service) that stay alive.

$ErrorActionPreference = 'Continue'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendDir = Join-Path $Root 'backend'

Write-Host '[1/3] Freeing ports 8000 / 5173 ...' -ForegroundColor Cyan
foreach ($port in @(8000, 5173)) {
    $listeners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        Write-Host ("  stopping process {0} on port {1}" -f $listener.OwningProcess, $port)
        Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
    }
}
Start-Sleep -Milliseconds 500

Write-Host '[2/3] Starting backend on http://localhost:8000 (new console window)' -ForegroundColor Cyan
Start-Process -FilePath 'cmd.exe' `
    -ArgumentList '/k', "cd /d `"$BackendDir`" && python -m uvicorn app.main:app --reload --port 8000" `
    -WorkingDirectory $BackendDir

Write-Host '[3/3] Starting frontend on http://localhost:5173 (new console window)' -ForegroundColor Cyan
Start-Process -FilePath 'cmd.exe' `
    -ArgumentList '/k', "cd /d `"$Root`" && npm run dev -- --host 0.0.0.0 --port 5173" `
    -WorkingDirectory $Root

Write-Host ''
Write-Host 'Waiting for services to come up ...' -ForegroundColor Cyan
$backendOk = $false
$frontendOk = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 500
    if (-not $backendOk) {
        try {
            $resp = Invoke-WebRequest -Uri 'http://localhost:8000/docs' -UseBasicParsing -TimeoutSec 2
            if ($resp.StatusCode -eq 200) { $backendOk = $true }
        } catch {}
    }
    if (-not $frontendOk) {
        try {
            $resp = Invoke-WebRequest -Uri 'http://localhost:5173/' -UseBasicParsing -TimeoutSec 2
            if ($resp.StatusCode -eq 200) { $frontendOk = $true }
        } catch {}
    }
    if ($backendOk -and $frontendOk) { break }
}

Write-Host ''
Write-Host 'Result:' -ForegroundColor Green
Write-Host ("  Backend  (8000): {0}" -f $(if ($backendOk) { 'OK  http://localhost:8000/docs' } else { 'FAILED - check the backend console window' }))
Write-Host ("  Frontend (5173): {0}" -f $(if ($frontendOk) { 'OK  http://localhost:5173' } else { 'FAILED - check the frontend console window' }))
Write-Host ''
Write-Host 'Keep the two console windows running. To stop, run stop-dev.cmd.' -ForegroundColor Yellow
