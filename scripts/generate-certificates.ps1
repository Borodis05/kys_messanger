param(
    [string]$ServerIp = "127.0.0.1",
    [string]$OutputDirectory = "certs",
    [switch]$ReuseCa
)

$ErrorActionPreference = "Stop"
$openssl = Get-Command openssl -ErrorAction Stop
if ([IO.Path]::IsPathRooted($OutputDirectory)) {
    $outputPath = [IO.Path]::GetFullPath($OutputDirectory)
} else {
    $outputPath = [IO.Path]::GetFullPath((Join-Path (Get-Location) $OutputDirectory))
}
New-Item -ItemType Directory -Path $outputPath -Force | Out-Null

$caConfig = Join-Path $outputPath "ca.cnf"
$serverConfig = Join-Path $outputPath "server.cnf"
$caKey = Join-Path $outputPath "ca.key"
$caCertificate = Join-Path $outputPath "ca.crt"
$serverKey = Join-Path $outputPath "server.key"
$serverRequest = Join-Path $outputPath "server.csr"
$serverCertificate = Join-Path $outputPath "server.crt"

@"
[req]
prompt = no
distinguished_name = distinguished_name

[distinguished_name]
CN = Corporate Chat Lab CA
"@ | Set-Content -LiteralPath $caConfig -Encoding ascii

@"
[req]
prompt = no
distinguished_name = distinguished_name
req_extensions = server_extensions

[distinguished_name]
CN = corporate-chat-server

[server_extensions]
subjectAltName = @alternative_names
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth

[alternative_names]
DNS.1 = localhost
IP.1 = 127.0.0.1
IP.2 = $ServerIp
"@ | Set-Content -LiteralPath $serverConfig -Encoding ascii

$hasReusableCa = $ReuseCa -and `
    (Test-Path -LiteralPath $caKey -PathType Leaf) -and `
    (Test-Path -LiteralPath $caCertificate -PathType Leaf)

if ($hasReusableCa) {
    Write-Output "Используется существующий локальный CA."
} else {
    & $openssl.Source req -x509 -newkey rsa:3072 -sha256 -days 3650 -nodes `
        -keyout $caKey -out $caCertificate -config $caConfig
    if ($LASTEXITCODE -ne 0) { throw "Не удалось создать локальный CA." }
}

& $openssl.Source req -newkey rsa:3072 -sha256 -nodes `
    -keyout $serverKey -out $serverRequest -config $serverConfig
if ($LASTEXITCODE -ne 0) { throw "Не удалось создать запрос сертификата сервера." }

if (Test-Path -LiteralPath (Join-Path $outputPath "ca.srl") -PathType Leaf) {
    & $openssl.Source x509 -req -in $serverRequest -CA $caCertificate `
        -CAkey $caKey -CAserial (Join-Path $outputPath "ca.srl") `
        -out $serverCertificate -days 825 -sha256 `
        -extensions server_extensions -extfile $serverConfig
} else {
    & $openssl.Source x509 -req -in $serverRequest -CA $caCertificate `
        -CAkey $caKey -CAcreateserial -out $serverCertificate -days 825 -sha256 `
        -extensions server_extensions -extfile $serverConfig
}
if ($LASTEXITCODE -ne 0) { throw "Не удалось подписать сертификат сервера." }

Remove-Item -LiteralPath $caConfig, $serverConfig, $serverRequest -Force

Write-Output "Сертификаты созданы в $outputPath"
Write-Output "Сервер использует server.crt и server.key."
Write-Output "Клиентам передаётся только ca.crt."
