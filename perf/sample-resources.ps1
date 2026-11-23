param([int]$Seconds = 240, [string]$OutFile)
# Samples the API's CPU and memory, PostgreSQL connections and Redis counters every 5 seconds.
# Adapted from the reference sampler for a multi-process server: the process listening on 8080 is traced up to
# the root of its Python process tree (venv launcher, Uvicorn supervisor), and CPU time and working sets are summed
# over the root and every descendant (all worker processes).
$psql = "$env:LOCALAPPDATA\HealthCareDev\pgsql\bin\psql.exe"
$redisCli = "C:\Program Files\Redis\redis-cli.exe"
$api = Get-NetTCPConnection -LocalPort 8080 -State Listen | Select-Object -First 1
$all = Get-CimInstance Win32_Process
$root = $all | Where-Object ProcessId -eq $api.OwningProcess
while ($true) {
    $parent = $all | Where-Object ProcessId -eq $root.ParentProcessId
    if ($parent -and $parent.Name -like "python*") { $root = $parent } else { break }
}
$rootId = $root.ProcessId
$cores = [Environment]::ProcessorCount

function Get-Tree {
    # Returns the root process and all of its descendants.
    $processes = Get-CimInstance Win32_Process
    $ids = @($rootId)
    $added = $true
    while ($added) {
        $added = $false
        foreach ($p in $processes) {
            if ($ids -notcontains $p.ProcessId -and $ids -contains $p.ParentProcessId) { $ids += $p.ProcessId; $added = $true }
        }
    }
    return Get-Process -Id $ids -ErrorAction SilentlyContinue
}

$tree = Get-Tree
$lastCpu = ($tree | ForEach-Object { $_.TotalProcessorTime.TotalSeconds } | Measure-Object -Sum).Sum
$lastTime = Get-Date
"time,cpu_percent_of_machine,working_set_mb,processes,pg_connections_app,pg_active_app" | Out-File $OutFile -Encoding utf8
$end = (Get-Date).AddSeconds($Seconds)
while ((Get-Date) -lt $end) {
    Start-Sleep -Seconds 5
    $tree = Get-Tree
    $now = Get-Date
    $cpu = ($tree | ForEach-Object { $_.TotalProcessorTime.TotalSeconds } | Measure-Object -Sum).Sum
    $percent = [math]::Round((($cpu - $lastCpu) / ($now - $lastTime).TotalSeconds) / $cores * 100, 1)
    $lastCpu = $cpu; $lastTime = $now
    $memory = [math]::Round((($tree | Measure-Object -Property WorkingSet64 -Sum).Sum) / 1MB, 0)
    $connections = & $psql -h localhost -p 54329 -U postgres -d postgres -Atc "select count(*), count(*) filter (where state = 'active') from pg_stat_activity where usename = 'healthcare_app'"
    $parts = $connections -split '\|'
    "$($now.ToString('HH:mm:ss')),$percent,$memory,$(@($tree).Count),$($parts[0]),$($parts[1])" | Out-File $OutFile -Append -Encoding utf8
}
$stats = & $redisCli INFO stats | Select-String "keyspace_hits|keyspace_misses"
$stats | ForEach-Object { "# redis $($_.Line.Trim())" } | Out-File $OutFile -Append -Encoding utf8
