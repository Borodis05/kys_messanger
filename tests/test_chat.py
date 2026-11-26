import asyncio
import unittest

from server.server import CLIENTS, handle_client
from shared.protocol import MAX_PACKET_SIZE, read_packet, write_packet


class ChatIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        CLIENTS.clear()
        self.server = await asyncio.start_server(
            handle_client, "127.0.0.1", 0, limit=MAX_PACKET_SIZE + 1
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self) -> None:
        self.server.close()
        await self.server.wait_closed()
        CLIENTS.clear()

    async def connect(self, name: str, request_id: str):
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", self.port, limit=MAX_PACKET_SIZE + 1
        )
        await write_packet(
            writer,
            {"type": "join", "request_id": request_id, "name": name},
        )
        response = await read_packet(reader)
        self.assertEqual(response["type"], "response")
        self.assertEqual(response["request_id"], request_id)
        self.assertTrue(response["ok"])
        return reader, writer

    async def test_json_message_is_broadcast_to_multiple_clients(self) -> None:
        alice_reader, alice_writer = await self.connect("alice", "join-alice")
        self.assertEqual((await read_packet(alice_reader))["event"], "user_joined")

        bob_reader, bob_writer = await self.connect("bob", "join-bob")
        self.assertEqual((await read_packet(alice_reader))["event"], "user_joined")
        self.assertEqual((await read_packet(bob_reader))["event"], "user_joined")

        await write_packet(
            alice_writer,
            {"type": "chat_message", "request_id": "msg-1", "text": "hello"},
        )

        response = await read_packet(alice_reader)
        self.assertEqual(response["request_id"], "msg-1")
        self.assertTrue(response["ok"])

        alice_message = await read_packet(alice_reader)
        bob_message = await read_packet(bob_reader)
        self.assertEqual(alice_message["type"], "message")
        self.assertEqual(alice_message["sender"], "alice")
        self.assertEqual(alice_message["text"], "hello")
        self.assertEqual(bob_message["text"], "hello")

        alice_writer.close()
        bob_writer.close()
        await alice_writer.wait_closed()
        await bob_writer.wait_closed()

    async def test_duplicate_name_returns_structured_error(self) -> None:
        alice_reader, alice_writer = await self.connect("alice", "join-1")
        await read_packet(alice_reader)

        duplicate_reader, duplicate_writer = await asyncio.open_connection(
            "127.0.0.1", self.port
        )
        await write_packet(
            duplicate_writer,
            {"type": "join", "request_id": "join-2", "name": "alice"},
        )

        response = await read_packet(duplicate_reader)
        self.assertEqual(response["type"], "error")
        self.assertEqual(response["code"], "name_taken")
        self.assertEqual(response["request_id"], "join-2")

        duplicate_writer.close()
        alice_writer.close()
        await duplicate_writer.wait_closed()
        await alice_writer.wait_closed()

    async def test_invalid_json_returns_error_without_disconnect(self) -> None:
        reader, writer = await self.connect("alice", "join-1")
        await read_packet(reader)

        writer.write(b"{invalid json}\n")
        await writer.drain()
        error = await read_packet(reader)
        self.assertEqual(error["type"], "error")
        self.assertEqual(error["code"], "invalid_json")

        await write_packet(
            writer,
            {"type": "chat_message", "request_id": "msg-2", "text": "still here"},
        )
        response = await read_packet(reader)
        self.assertEqual(response["request_id"], "msg-2")
        self.assertTrue(response["ok"])

        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
