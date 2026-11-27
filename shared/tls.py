import ssl
from pathlib import Path


class TLSConfigurationError(RuntimeError):
    pass


def require_file(path: str, description: str) -> str:
    resolved_path = Path(path)
    if not resolved_path.is_file():
        raise TLSConfigurationError(
            f"{description} не найден: {resolved_path}. "
            "Сначала создайте сертификаты по инструкции в certs/README.md."
        )
    return str(resolved_path)


def create_server_tls_context(certfile: str, keyfile: str) -> ssl.SSLContext:
    certificate = require_file(certfile, "Сертификат сервера")
    private_key = require_file(keyfile, "Закрытый ключ сервера")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(certfile=certificate, keyfile=private_key)
    except ssl.SSLError as error:
        raise TLSConfigurationError(
            f"Не удалось загрузить сертификат или закрытый ключ: {error}"
        ) from error
    return context


def create_client_tls_context(cafile: str) -> ssl.SSLContext:
    ca_certificate = require_file(cafile, "Сертификат центра сертификации")
    try:
        context = ssl.create_default_context(
            purpose=ssl.Purpose.SERVER_AUTH,
            cafile=ca_certificate,
        )
    except ssl.SSLError as error:
        raise TLSConfigurationError(
            f"Не удалось загрузить сертификат центра сертификации: {error}"
        ) from error

    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context
