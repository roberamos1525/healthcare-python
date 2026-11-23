param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string]$Script,
    [string]$Profile = "",
    [int]$Contenders = 0,
    [int]$SampleSeconds = 0,
    [int]$Workers = 8,
    [int]$PoolSize = 15,
    [string]$LogLevel = "Warning",
    [string]$KeyDirectory = "$env:LOCALAPPDATA\HealthCareDev\keys-python",
    [string]$Database = "healthcare_python",
    [string]$Psql = "$env:LOCALAPPDATA\HealthCareDev\pgsql\bin\psql.exe",
    [string]$RedisCli = "C:\Program Files\Redis\redis-cli.exe",
    [string]$K6 = "C:\Program Files\k6\k6.exe"
)
# Uvicorn listens on IPv4 only, and "localhost" resolves to ::1 first on Windows (a 2 s fallback per connection),
# so the runner addresses the API (and Redis, which also listens on IPv4 only) as 127.0.0.1.
# Runs one measured k6 scenario the way the numbers in docs/testing.md were produced: stops whatever listens on
# 8080 (and its process tree), starts a fresh production-style server (Uvicorn with $Workers worker processes,
# Production settings, keys read from files outside the repository, Redis, background workers on, rate limiting
# off, email to Mailpit on 127.0.0.1:1025), waits for /health/ready, resets the Redis statistics, runs k6 with
# optional resource sampling and records the outbox lag afterwards. Output goes to perf/results/<Name>*.
#   ./perf/run-measured.ps1 -Name smoke -Script smoke.js
#   ./perf/run-measured.ps1 -Name contention -Script booking-contention.js -Contenders 50
#   ./perf/run-measured.ps1 -Name baseline -Script load.js -Profile baseline -SampleSeconds 215
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$results = Join-Path $PSScriptRoot "results"
New-Item -ItemType Directory -Force $results | Out-Null

function Stop-Tree([int]$ProcessId) {
    # Stops a process and all of its descendants.
    Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" | ForEach-Object { Stop-Tree $_.ProcessId }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

$listener = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    $top = $process
    while ($true) {
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($top.ParentProcessId)"
        if ($parent -and $parent.Name -like "python*") { $top = $parent } else { break }
    }
    Stop-Tree $top.ProcessId
    Start-Sleep -Seconds 2
}

$environment = @{
    ASPNETCORE_ENVIRONMENT          = "Production"
    ConnectionStrings__Database     = "Host=localhost;Port=54329;Database=$Database;Username=healthcare_app;Maximum Pool Size=$PoolSize"
    ConnectionStrings__Redis        = "127.0.0.1:6379"
    Auth__SigningKeyPath            = (Join-Path $KeyDirectory "signing.pem")
    Auth__FieldEncryptionKey        = (Get-Content (Join-Path $KeyDirectory "field.key") -Raw).Trim()
    Email__Provider                 = "Smtp"
    Email__SmtpHost                 = "127.0.0.1"
    Email__SmtpPort                 = "1025"
    RateLimiting__Enabled           = "false"
    Messaging__Provider             = "Log"
    Workers__Enabled                = "true"
    Serilog__MinimumLevel__Default  = $LogLevel
    HEALTHCARE_WORKERS              = "$Workers"
    PORT                            = "8080"
}
foreach ($entry in $environment.GetEnumerator()) { Set-Item -Path "Env:$($entry.Key)" -Value $entry.Value }
$python = Join-Path $root ".venv\Scripts\python.exe"
Start-Process -FilePath $python -ArgumentList "-m", "healthcare" -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $results "$Name-api.log") -RedirectStandardError (Join-Path $results "$Name-api.err")

$ready = $false
for ($i = 0; $i -lt 120; $i++) {
    Start-Sleep -Milliseconds 500
    try { if ((Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8080/health/ready -TimeoutSec 2).StatusCode -eq 200) { $ready = $true; break } } catch {}
}
if (-not $ready) { throw "the API did not become ready" }
Start-Sleep -Seconds 3
& $RedisCli CONFIG RESETSTAT | Out-Null

$sampler = $null
if ($SampleSeconds -gt 0) {
    $sampler = Start-Job -ScriptBlock { param($s, $n, $o) & powershell -NoProfile -File $s -Seconds $n -OutFile $o } `
        -ArgumentList (Join-Path $PSScriptRoot "sample-resources.ps1"), $SampleSeconds, (Join-Path $results "$Name-resources.csv")
}
$arguments = @("run", "-e", "BASE_URL=http://127.0.0.1:8080", "-e", "MAILPIT_URL=http://127.0.0.1:8025", "--summary-export", (Join-Path $results "$Name-summary.json"))
if ($Profile) { $arguments += @("-e", "PROFILE=$Profile") }
if ($Contenders -gt 0) { $arguments += @("-e", "CONTENDERS=$Contenders") }
$arguments += (Join-Path $PSScriptRoot "k6\$Script")
$env:K6_NO_COLOR = "true"
& $K6 @arguments *> (Join-Path $results "$Name.txt")
"k6 exit code: $LASTEXITCODE" | Out-File (Join-Path $results "$Name.txt") -Append -Encoding utf8
if ($sampler) { Wait-Job $sampler | Out-Null; Receive-Job $sampler | Out-Null; Remove-Job $sampler }

if ($SampleSeconds -gt 0) {
    Start-Sleep -Seconds 5
    $query = "select count(*) as events, count(*) filter (where processed_at is null) as pending, " +
        "round(avg(extract(epoch from processed_at - occurred_at))::numeric, 2) as lag_mean_s, " +
        "round((percentile_cont(0.95) within group (order by extract(epoch from processed_at - occurred_at)))::numeric, 2) as lag_p95_s, " +
        "round(max(extract(epoch from processed_at - occurred_at))::numeric, 2) as lag_max_s " +
        "from platform.outbox_events where occurred_at > now() - interval '6 minutes'"
    & $Psql -h localhost -p 54329 -U postgres -d $Database -c $query *> (Join-Path $results "$Name-outbox.txt")
}
