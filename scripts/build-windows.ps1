param(
    [string]$PythonDirectory = ".venv",
    [string]$ServerIp = ""
)

$ErrorActionPreference = "Stop"
$workspacePath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$pyInstaller = Join-Path $workspacePath "$PythonDirectory\Scripts\pyinstaller.exe"

if (-not (Test-Path -LiteralPath $pyInstaller -PathType Leaf)) {
    throw "PyInstaller не найден. Выполните: .\.venv\Scripts\python -m pip install -r requirements-dev.txt"
}

if ([string]::IsNullOrWhiteSpace($ServerIp)) {
    try {
        $defaultRoutes = Get-NetRoute -AddressFamily IPv4 `
            -DestinationPrefix "0.0.0.0/0" -ErrorAction Stop |
            Sort-Object RouteMetric
        foreach ($route in $defaultRoutes) {
            $address = Get-NetIPAddress -AddressFamily IPv4 `
                -InterfaceIndex $route.InterfaceIndex -ErrorAction SilentlyContinue |
                Where-Object {
                    $_.AddressState -eq "Preferred" -and
                    $_.IPAddress -ne "127.0.0.1" -and
                    -not $_.IPAddress.StartsWith("169.254.")
                } |
                Select-Object -First 1
            if ($null -ne $address) {
                $ServerIp = $address.IPAddress
                break
            }
        }
    } catch {
        $ServerIp = ""
    }
}

if ([string]::IsNullOrWhiteSpace($ServerIp)) {
    $ServerIp = "127.0.0.1"
    Write-Warning "Сетевой IP не найден; сертификат создаётся только для локального запуска."
}

$parsedServerIp = $null
if (-not [Net.IPAddress]::TryParse($ServerIp, [ref]$parsedServerIp)) {
    throw "Некорректный IP-адрес сервера: $ServerIp"
}

Write-Output "IP сервера для сертификата: $ServerIp"
& (Join-Path $PSScriptRoot "generate-certificates.ps1") `
    -ServerIp $ServerIp `
    -OutputDirectory (Join-Path $workspacePath "certs") `
    -ReuseCa
if ($LASTEXITCODE -ne 0) { throw "Не удалось подготовить сертификаты." }

$requiredFiles = @(
    "certs\ca.crt",
    "certs\server.crt",
    "certs\server.key",
    "packaging\CLIENT_README.txt",
    "packaging\SERVER_README.txt"
)
foreach ($relativePath in $requiredFiles) {
    $fullPath = Join-Path $workspacePath $relativePath
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        throw "Отсутствует обязательный файл: $relativePath"
    }
}

$buildPath = Join-Path $workspacePath "build"
$releasePath = Join-Path $workspacePath "release"
$clientPath = Join-Path $releasePath "ChatClient"
$serverPath = Join-Path $releasePath "ChatServer"

foreach ($targetPath in @($buildPath, $clientPath, $serverPath)) {
    $absoluteTarget = [IO.Path]::GetFullPath($targetPath)
    if (-not $absoluteTarget.StartsWith($workspacePath + [IO.Path]::DirectorySeparatorChar)) {
        throw "Небезопасный путь сборки: $absoluteTarget"
    }
    if (Test-Path -LiteralPath $absoluteTarget) {
        Remove-Item -LiteralPath $absoluteTarget -Recurse -Force
    }
}

New-Item -ItemType Directory -Path $clientPath, $serverPath -Force | Out-Null

& $pyInstaller --noconfirm --clean --onefile --console `
    --name ChatClient `
    --paths $workspacePath `
    --distpath $clientPath `
    --workpath (Join-Path $buildPath "client") `
    --specpath (Join-Path $buildPath "spec") `
    (Join-Path $workspacePath "client\client.py")
if ($LASTEXITCODE -ne 0) { throw "Не удалось собрать ChatClient.exe" }

& $pyInstaller --noconfirm --clean --onefile --console `
    --name ChatServer `
    --paths $workspacePath `
    --distpath $serverPath `
    --workpath (Join-Path $buildPath "server") `
    --specpath (Join-Path $buildPath "spec") `
    (Join-Path $workspacePath "server\server.py")
if ($LASTEXITCODE -ne 0) { throw "Не удалось собрать ChatServer.exe" }

New-Item -ItemType Directory -Path `
    (Join-Path $clientPath "certs"), `
    (Join-Path $serverPath "certs") -Force | Out-Null

Copy-Item -LiteralPath (Join-Path $workspacePath "certs\ca.crt") `
    -Destination (Join-Path $clientPath "certs\ca.crt")
Copy-Item -LiteralPath (Join-Path $workspacePath "certs\server.crt") `
    -Destination (Join-Path $serverPath "certs\server.crt")
Copy-Item -LiteralPath (Join-Path $workspacePath "certs\server.key") `
    -Destination (Join-Path $serverPath "certs\server.key")
Copy-Item -LiteralPath (Join-Path $workspacePath "packaging\CLIENT_README.txt") `
    -Destination (Join-Path $clientPath "README.txt")
Copy-Item -LiteralPath (Join-Path $workspacePath "packaging\SERVER_README.txt") `
    -Destination (Join-Path $serverPath "README.txt")

$clientArchive = Join-Path $releasePath "ChatClient-windows-x64.zip"
$serverArchive = Join-Path $releasePath "ChatServer-windows-x64.zip"
Compress-Archive -Path (Join-Path $clientPath "*") -DestinationPath $clientArchive -Force
Compress-Archive -Path (Join-Path $serverPath "*") -DestinationPath $serverArchive -Force

Write-Output "Сборка готова:"
Write-Output "  $clientArchive"
Write-Output "  $serverArchive"
Write-Output "Закрытый ключ CA (ca.key) в архивы не добавлен."
