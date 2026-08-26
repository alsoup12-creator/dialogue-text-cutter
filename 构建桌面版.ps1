$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktopProject = Join-Path $projectRoot "desktop\TextCut.Desktop.csproj"
$publishFolder = Join-Path $projectRoot "desktop\publish"

dotnet restore $desktopProject -p:NuGetAudit=false
if ($LASTEXITCODE -ne 0) {
  throw "Desktop dependency restore failed. Check the network and NuGet configuration."
}

dotnet publish $desktopProject `
  --configuration Release `
  --self-contained false `
  --output $publishFolder `
  --no-restore `
  -p:PublishSingleFile=true `
  -p:DebugType=None `
  -p:DebugSymbols=false
if ($LASTEXITCODE -ne 0) {
  throw "Desktop build failed. Review the error output above."
}

Write-Host ""
Write-Host "Desktop build created in: $publishFolder"
