$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktopProject = Join-Path $projectRoot "desktop\TextCut.Desktop.csproj"
$publishFolder = Join-Path $projectRoot "desktop\publish"
$stageFolder = Join-Path $projectRoot ("desktop\publish-stage-" + [Guid]::NewGuid().ToString("N"))

dotnet restore $desktopProject -p:NuGetAudit=false
if ($LASTEXITCODE -ne 0) {
  throw "Desktop dependency restore failed. Check the network and NuGet configuration."
}

try {
  dotnet publish $desktopProject `
    --configuration Release `
    --self-contained false `
    --output $stageFolder `
    --no-restore `
    -p:PublishSingleFile=true `
    -p:DebugType=None `
    -p:DebugSymbols=false
  if ($LASTEXITCODE -ne 0) {
    throw "Desktop build failed. Review the error output above."
  }

  $requiredFiles = @(
    (Join-Path $stageFolder "对白剪辑台.exe"),
    (Join-Path $stageFolder "WebView2Loader.dll")
  )
  foreach ($file in $requiredFiles) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
      throw "Desktop build is incomplete: missing $file"
    }
  }

  if (Test-Path -LiteralPath $publishFolder) {
    $backupFolder = Join-Path $projectRoot ("desktop\publish-backup-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
    Move-Item -LiteralPath $publishFolder -Destination $backupFolder
  }
  Move-Item -LiteralPath $stageFolder -Destination $publishFolder
}
finally {
  if (Test-Path -LiteralPath $stageFolder) {
    Remove-Item -LiteralPath $stageFolder -Recurse -Force
  }
}

Write-Host ""
Write-Host "Desktop build created in: $publishFolder"
