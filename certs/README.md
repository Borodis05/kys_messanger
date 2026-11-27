# Сертификаты TLS

Сервер принимает только TLS-подключения. Перед первым запуском создайте
локальный центр сертификации и серверный сертификат из корня проекта:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/generate-certificates.ps1
```

Для сервера в локальной сети укажите его постоянный IP:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/generate-certificates.ps1 `
  -ServerIp 192.168.1.10
```

Файлы `server.crt` и `server.key` должны оставаться только на сервере. Клиентам
нужно безопасно передать лишь `ca.crt` и положить его в папку `certs` проекта.
Закрытые ключи и сертификаты исключены из Git.
