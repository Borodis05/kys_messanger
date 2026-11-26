import asyncio
import unittest

from server.server import CLIENTS, handle_client


class ChatIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        CLIENTS.clear()
        self.server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self) -> None:
        self.server.close()
        await self.server.wait_closed()
        CLIENTS.clear()

    async def connect(self, name: str):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(f"{name}\n".encode("utf-8"))
        await writer.drain()
        self.assertEqual(await reader.readline(), b"OK\n")
        return reader, writer

    async def test_message_is_broadcast_to_multiple_clients(self) -> None:
        alice_reader, alice_writer = await self.connect("alice")
        self.assertIn(b"alice", await alice_reader.readline())

        bob_reader, bob_writer = await self.connect("bob")
        self.assertIn(b"bob", await alice_reader.readline())
        self.assertIn(b"bob", await bob_reader.readline())

        alice_writer.write(b"hello\n")
        await alice_writer.drain()

        self.assertEqual(await alice_reader.readline(), b"[alice] hello\n")
        self.assertEqual(await bob_reader.readline(), b"[alice] hello\n")

        alice_writer.close()
        bob_writer.close()
        await alice_writer.wait_closed()
        await bob_writer.wait_closed()

    async def test_duplicate_name_is_rejected(self) -> None:
        alice_reader, alice_writer = await self.connect("alice")
        await alice_reader.readline()

        duplicate_reader, duplicate_writer = await asyncio.open_connection(
            "127.0.0.1", self.port
        )
        duplicate_writer.write(b"alice\n")
        await duplicate_writer.drain()

        response = await duplicate_reader.readline()
        self.assertTrue(response.startswith(b"ERROR"))

        duplicate_writer.close()
        alice_writer.close()
        await duplicate_writer.wait_closed()
        await alice_writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
