$ErrorActionPreference = "Stop"
$baseUrl = if ($env:ATA_BASE_URL) { $env:ATA_BASE_URL } else { "http://localhost:8080" }
$health = Invoke-RestMethod -Uri "$baseUrl/api/health"
if ($health.status -ne "ok") { throw "health check failed" }
$incident = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/demo/seed"
if (-not $incident.incident_id) { throw "demo seed did not create an incident" }
$runtime = Invoke-RestMethod -Uri "$baseUrl/api/runtime"
Write-Output "smoke ok: $($incident.incident_id) status=$($incident.status) deployment=$($runtime.deployment)"
