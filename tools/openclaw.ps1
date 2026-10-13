# Use ATHENA's private OpenClaw runtime and existing provider credentials.
$athenaProject = Split-Path -Parent $PSScriptRoot
$athenaPython = Join-Path $athenaProject '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $athenaPython)) {
    throw 'ATHENA Python environment is missing. Install ATHENA first.'
}
& $athenaPython -m athena.openclaw @args
exit $LASTEXITCODE
