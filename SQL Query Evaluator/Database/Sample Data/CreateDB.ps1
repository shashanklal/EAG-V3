# create_access_db.ps1
# Creates an Access .accdb and imports 4 CSVs as tables named after CSV base names.
# Requirements: Windows + Microsoft Access installed.

param(
  [string]$DbPath = "$(Join-Path $PSScriptRoot 'SyntheticClaims.accdb')",
  [string]$Folder = "$PSScriptRoot"
)

$csvs = @(
  "synthetic_claims_claims.csv",
  "synthetic_claims_lines.csv",
  "synthetic_claims_events.csv",
  "synthetic_claims_flat.csv"
)

$access = New-Object -ComObject Access.Application
$access.Visible = $false

if (Test-Path $DbPath) { Remove-Item $DbPath -Force }
$access.NewCurrentDatabase($DbPath)

foreach ($csv in $csvs) {
  $path = Join-Path $Folder $csv
  if (!(Test-Path $path)) { throw "CSV not found: $path" }

  $tableName = [System.IO.Path]::GetFileNameWithoutExtension($csv)

  # acImportDelim = 0; hasfieldnames = $true
  $access.DoCmd.TransferText(0, "", $tableName, $path, $true)
}

$access.CloseCurrentDatabase()
$access.Quit()
[System.Runtime.InteropServices.Marshal]::ReleaseComObject($access) | Out-Null

Write-Host "Created: $DbPath"