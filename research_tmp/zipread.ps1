param(
  [Parameter(Mandatory=$true)][string]$Entry,
  [int]$Head = 0,
  [int]$Tail = 0,
  [string]$Grep = '',
  [int]$Context = 0
)
Add-Type -AssemblyName System.IO.Compression.FileSystem
$z = [System.IO.Compression.ZipFile]::OpenRead('E:\workspace\_archive\量化-精简前-20261123-185555.zip')
try {
  $e = $z.GetEntry($Entry)
  if (-not $e) { Write-Error "ENTRY NOT FOUND: $Entry"; exit 1 }
  $sr = New-Object System.IO.StreamReader($e.Open(), [System.Text.Encoding]::UTF8)
  $text = $sr.ReadToEnd()
  $sr.Dispose()
  if ($Grep) {
    $lines = $text -split "`r?`n"
    $hits = for ($i=0; $i -lt $lines.Count; $i++) { if ($lines[$i] -match $Grep) { $i } }
    $shown = New-Object System.Collections.Generic.HashSet[int]
    foreach ($h in $hits) {
      for ($j=[Math]::Max(0,$h-$Context); $j -le [Math]::Min($lines.Count-1,$h+$Context); $j++) { [void]$shown.Add($j) }
    }
    foreach ($j in ($shown | Sort-Object)) { "{0,5}: {1}" -f ($j+1), $lines[$j] }
  } elseif ($Head -gt 0 -and $Tail -gt 0) {
    $lines = $text -split "`r?`n"
    $h = [Math]::Min($Head,$lines.Count)
    $t = [Math]::Min($Tail,[Math]::Max(0,$lines.Count-$h))
    for ($i=0; $i -lt $h; $i++) { "{0,5}: {1}" -f ($i+1), $lines[$i] }
    "   ... ($($lines.Count-$h-$t) lines omitted) ..."
    for ($i=$lines.Count-$t; $i -lt $lines.Count; $i++) { "{0,5}: {1}" -f ($i+1), $lines[$i] }
  } elseif ($Head -gt 0) {
    $lines = $text -split "`r?`n"
    $h = [Math]::Min($Head,$lines.Count)
    for ($i=0; $i -lt $h; $i++) { "{0,5}: {1}" -f ($i+1), $lines[$i] }
    if ($lines.Count -gt $h) { "   ... ($($lines.Count-$h) more lines) ..." }
  } elseif ($Tail -gt 0) {
    $lines = $text -split "`r?`n"
    $t = [Math]::Min($Tail,$lines.Count)
    for ($i=$lines.Count-$t; $i -lt $lines.Count; $i++) { "{0,5}: {1}" -f ($i+1), $lines[$i] }
  } else {
    $text
  }
} finally { $z.Dispose() }
