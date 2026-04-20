# Deploy and serve DEEP RED Stories
# Creates a site\ folder, copies web assets + pipeline output, then launches a test server.

$Root = Split-Path -Parent $PSScriptRoot
$Site = Join-Path $Root "site"

Write-Host "============================================"
Write-Host " DEEP RED Stories - Build and Serve"
Write-Host "============================================"

# --- Clean previous build ---
if (Test-Path $Site) {
    Write-Host "Removing previous site folder..."
    Remove-Item -Recurse -Force $Site
}

# --- Create site folder ---
Write-Host "Creating site folder..."
New-Item -ItemType Directory -Force $Site | Out-Null

# --- Copy web content ---
Write-Host "Copying web assets..."
Copy-Item -Recurse -Force (Join-Path $Root "web\*") $Site

# --- Copy pipeline output into site\data ---
Write-Host "Copying pipeline output..."
$DataDir = Join-Path $Site "data"
if (Test-Path $DataDir) {
    Remove-Item -Recurse -Force $DataDir
}
New-Item -ItemType Directory -Force $DataDir | Out-Null
Copy-Item -Force (Join-Path $Root "pipeline\output\index.json") $DataDir
Copy-Item -Recurse -Force (Join-Path $Root "pipeline\output\games") (Join-Path $DataDir "games")

Write-Host ""
Write-Host "Build complete: $Site"
Write-Host "============================================"
Write-Host "Serving at http://localhost:8000"
Write-Host "Press Ctrl+C to stop."
Write-Host "============================================"

Set-Location $Site
python -m http.server 8000
