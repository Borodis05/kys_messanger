import asyncio
import ssl
import unittest
from pathlib import Path

from shared.tls import (
    TLSConfigurationError,
    create_client_tls_context,
    create_server_tls_context,
)


CERTIFICATE = Path("certs/server.crt")
PRIVATE_KEY = Path("certs/server.key")
CA_CERTIFICATE = Path("certs/ca.crt")


class TLSConfigurationTest(unittest.TestCase):
    def test_missing_certificate_is_rejected(self) -> None:
        with self.assertRaises(TLSConfigurationError):
            create_client_tls_context("certs/missing-ca.crt")

    def test_contexts_require_tls_twelve_or_newer(self) -> None:
        if not all(path.is_file() for path in (CERTIFICATE, PRIVATE_KEY, CA_CERTIFICATE)):
            self.skipTest("Локальные тестовые сертификаты ещё не созданы.")

        server_context = create_server_tls_context(
            str(CERTIFICATE), str(PRIVATE_KEY)
        )
        client_context = create_client_tls_context(str(CA_CERTIFICATE))
        self.assertEqual(server_context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(client_context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertTrue(client_context.check_hostname)
        self.assertEqual(client_context.verify_mode, ssl.CERT_REQUIRED)


class TLSHandshakeTest(unittest.IsolatedAsyncioTestCase):
    async def test_verified_tls_handshake(self) -> None:
        if not all(path.is_file() for path in (CERTIFICATE, PRIVATE_KEY, CA_CERTIFICATE)):
            self.skipTest("Локальные тестовые сертификаты ещё не созданы.")

        server_context = create_server_tls_context(
            str(CERTIFICATE), str(PRIVATE_KEY)
        )
        client_context = create_client_tls_context(str(CA_CERTIFICATE))

        async def echo(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            writer.write(await reader.readline())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(
            echo, "127.0.0.1", 0, ssl=server_context
        )
        port = server.sockets[0].getsockname()[1]

        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1",
                port,
                ssl=client_context,
                server_hostname="localhost",
            )
            tls_session = writer.get_extra_info("ssl_object")
            self.assertIn(tls_session.version(), {"TLSv1.2", "TLSv1.3"})

            writer.write(b"protected\n")
            await writer.drain()
            self.assertEqual(await reader.readline(), b"protected\n")
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
