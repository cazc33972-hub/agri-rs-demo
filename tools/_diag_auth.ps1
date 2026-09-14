$ErrorActionPreference = 'Continue'

'=== Browser windows ==='
Get-Process chrome, msedge, firefox -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowTitle } |
    Select-Object ProcessName, Id, MainWindowTitle |
    Format-Table -AutoSize | Out-String

'=== Default browser (https) ==='
$k = 'HKCU:\SOFTWARE\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice'
(Get-ItemProperty -Path $k -ErrorAction SilentlyContinue).ProgId

'=== GCM log dir ==='
$g = Join-Path $env:LOCALAPPDATA '.gcm'
if (Test-Path $g) {
    Get-ChildItem $g -Recurse -File -ErrorAction SilentlyContinue |
        Select-Object -Last 5 |
        ForEach-Object { $_.FullName + '  ' + $_.Length + ' bytes' }
} else { 'none (trace not enabled)' }

'=== GCM process ==='
$p = Get-Process git-credential-manager -ErrorAction SilentlyContinue
if ($p) {
    foreach ($proc in $p) {
        'PID ' + $proc.Id + ' running ' + [math]::Round(((Get-Date) - $proc.StartTime).TotalMinutes, 1) + ' min  window=[' + $proc.MainWindowTitle + ']'
    }
} else { 'exited' }
