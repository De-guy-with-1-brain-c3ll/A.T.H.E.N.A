param([switch]$Autostart)
$athenaSource = Split-Path -Parent $PSScriptRoot
$athenaPython = Join-Path $athenaSource '.venv/Scripts/python.exe'
$athenaClientRoot = Join-Path $env:USERPROFILE '.athena'
$athenaClientEnv = Join-Path $athenaClientRoot 'venv'
New-Item -ItemType Directory -Force -Path $athenaClientRoot | Out-Null
$athenaClientPython = Join-Path $athenaClientEnv 'Scripts/python.exe'
if (-not (Test-Path -LiteralPath $athenaClientPython)) {
    & $athenaPython -m venv $athenaClientEnv
    if ($LASTEXITCODE -ne 0) { throw 'Console environment creation failed.' }
}
if (Test-Path -LiteralPath (Join-Path $athenaClientEnv 'Scripts/athena.exe')) {
    # Updating pure Python modules does not rewrite the running Windows launcher.
    # Existing consoles adopt the new dashboard when reopened.
    foreach ($athenaModule in @('athena_console.py','athena_dashboard.py')) {
        Copy-Item -LiteralPath (Join-Path $athenaSource ('packages/athena-console/'+$athenaModule)) -Destination (Join-Path $athenaClientEnv ('Lib/site-packages/'+$athenaModule)) -Force
    }
} else {
    & $athenaClientPython -m pip install --no-deps (Join-Path $athenaSource 'packages/athena-console')
    if ($LASTEXITCODE -ne 0) { throw 'Console installation failed.' }
}
$athenaPointer = @{source=$athenaSource; python=$athenaPython; control=(Join-Path $athenaSource 'data/openclaw/athena-control.json')}
$athenaPointer | ConvertTo-Json | Set-Content -Encoding utf8 -LiteralPath (Join-Path $athenaClientRoot 'connection.json')
$athenaBin = Join-Path $athenaClientEnv 'Scripts'
$athenaUserPath = [Environment]::GetEnvironmentVariable('Path','User')
if (($athenaUserPath -split ';') -notcontains $athenaBin) {
    [Environment]::SetEnvironmentVariable('Path',($athenaUserPath.TrimEnd(';')+';'+$athenaBin),'User')
}
$env:Path += ';'+$athenaBin
if ($Autostart) {
    $athenaExe = Join-Path $athenaBin 'athena.exe'
    $athenaUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $athenaAction = New-ScheduledTaskAction -Execute (Join-Path $athenaClientEnv 'Scripts/pythonw.exe') -Argument ('"'+(Join-Path $athenaSource 'tools/athena_login.py')+'"')
    $athenaTrigger = New-ScheduledTaskTrigger -AtLogOn -User $athenaUser
    $athenaPrincipal = New-ScheduledTaskPrincipal -UserId $athenaUser -LogonType Interactive -RunLevel Limited
    $athenaSettings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName 'ATHENA Core' -Action $athenaAction -Trigger $athenaTrigger -Principal $athenaPrincipal -Settings $athenaSettings -Force | Out-Null
}
Write-Output 'ATHENA console installed. Open a new terminal and use athena start. Microphone listening stays off.'
