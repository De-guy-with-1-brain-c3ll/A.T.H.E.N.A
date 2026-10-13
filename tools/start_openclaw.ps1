$athenaProject = Split-Path -Parent $PSScriptRoot
$athenaPython = Join-Path $athenaProject '.venv/Scripts/python.exe'
$athenaData = Join-Path $athenaProject 'data/openclaw'
New-Item -ItemType Directory -Force -Path $athenaData | Out-Null
if (Get-NetTCPConnection -LocalPort 18789 -State Listen -ErrorAction SilentlyContinue) {
    Write-Output 'Port 18789 already has a listener. Check OpenClaw health before starting another gateway.'
    exit 0
}
$athenaGateway = Start-Process -FilePath $athenaPython -ArgumentList '-m','athena.openclaw','gateway','run' -WorkingDirectory $athenaProject -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $athenaData 'gateway.stdout.log') -RedirectStandardError (Join-Path $athenaData 'gateway.stderr.log')
$athenaGateway.Id | Set-Content -LiteralPath (Join-Path $athenaData 'launcher.pid')
Write-Output 'OpenClaw gateway is starting. Use tools/openclaw.ps1 health to check readiness.'
