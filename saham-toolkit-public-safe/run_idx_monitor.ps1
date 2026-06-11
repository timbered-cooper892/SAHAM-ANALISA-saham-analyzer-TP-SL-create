param(
    [ValidateSet("news", "fundamental", "full")]
    [string]$Mode = "news",
    [int]$Days = 1,
    [int]$MaxRecords = 5,
    [int]$Offset = 0,
    [Nullable[int]]$Limit = $null,
    [double]$Sleep = 1.0,
    [switch]$UseGdelt,
    [switch]$NoMacro
)

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$ArgsList = @(
    (Join-Path $Root "scripts\monitor_idx.py"),
    "--mode", $Mode,
    "--days", $Days,
    "--max-records", $MaxRecords,
    "--offset", $Offset,
    "--sleep", $Sleep
)

if ($Limit -ne $null) {
    $ArgsList += @("--limit", $Limit)
}

if (-not $UseGdelt) {
    $ArgsList += "--no-gdelt"
}

if ($NoMacro) {
    $ArgsList += "--no-macro"
}

& $Python @ArgsList

