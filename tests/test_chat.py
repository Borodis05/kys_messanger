import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from client.client import prompt_password
from server import server as chat_server
from server.auth import verify_password
from shared.protocol import MAX_PACKET_SIZE, read_packet, write_packet


PASSWORD = "strong-password"


class ChatIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        chat_server.CLIENTS.clear()
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = str(Path(self.temporary_directory.name) / "test.db")
        chat_server.configure_database(database_path)
        self.server = await asyncio.start_server(
            chat_server.handle_client,
            "127.0.0.1",
            0,
            limit=MAX_PACKET_SIZE + 1,
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self) -> None:
        self.server.close()
        await self.server.wait_closed()
        await self.wait_until_offline()
        chat_server.CLIENTS.clear()
        self.temporary_directory.cleanup()

    async def open_connection(self):
        return await asyncio.open_connection(
            "127.0.0.1", self.port, limit=MAX_PACKET_SIZE + 1
        )

    async def submit_username(
        self,
        action: str,
        username: str,
        request_id: str,
        connection=None,
    ):
        if connection is None:
            reader, writer = await self.open_connection()
        else:
            reader, writer = connection
        await write_packet(
            writer,
            {
                "type": "auth_username",
                "action": action,
                "request_id": request_id,
                "username": username,
            },
        )
        return reader, writer, await read_packet(reader)

    async def submit_password(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        password: str,
        request_id: str,
    ):
        await write_packet(
            writer,
            {
                "type": "auth_password",
                "request_id": request_id,
                "password": password,
            },
        )
        return await read_packet(reader)

    async def authenticate(
        self, action: str, username: str, request_prefix: str
    ):
        reader, writer, username_response = await self.submit_username(
            action, username, f"{request_prefix}-username"
        )
        self.assertEqual(username_response["type"], "response")
        self.assertEqual(username_response["action"], "auth_username")

        password_response = await self.submit_password(
            reader, writer, PASSWORD, f"{request_prefix}-password"
        )
        self.assertEqual(password_response["type"], "response")
        self.assertEqual(password_response["action"], action)
        return reader, writer

    async def disconnect(self, writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()
        await self.wait_until_offline()

    async def wait_until_offline(self) -> None:
        for _ in range(100):
            if not chat_server.CLIENTS:
                return
            await asyncio.sleep(0.01)

    async def test_registered_users_can_exchange_messages(self) -> None:
        alice_reader, alice_writer = await self.authenticate(
            "register", "Alice", "alice"
        )
        self.assertEqual((await read_packet(alice_reader))["event"], "user_joined")

        bob_reader, bob_writer = await self.authenticate("register", "Bob", "bob")
        self.assertEqual((await read_packet(alice_reader))["event"], "user_joined")
        self.assertEqual((await read_packet(bob_reader))["event"], "user_joined")

        await write_packet(
            alice_writer,
            {"type": "chat_message", "request_id": "msg-1", "text": "hello"},
        )
        self.assertEqual((await read_packet(alice_reader))["request_id"], "msg-1")
        self.assertEqual((await read_packet(alice_reader))["text"], "hello")
        self.assertEqual((await read_packet(bob_reader))["text"], "hello")

        alice_writer.close()
        bob_writer.close()
        await alice_writer.wait_closed()
        await bob_writer.wait_closed()

    async def test_duplicate_username_is_rejected_before_password(self) -> None:
        alice_reader, alice_writer = await self.authenticate(
            "register", "Alice", "alice"
        )
        await read_packet(alice_reader)

        _, duplicate_writer, response = await self.submit_username(
            "register", "alice", "duplicate-username"
        )
        self.assertEqual(response["type"], "error")
        self.assertEqual(response["code"], "user_exists")
        self.assertEqual(response["request_id"], "duplicate-username")

        duplicate_writer.close()
        await duplicate_writer.wait_closed()
        await self.disconnect(alice_writer)

    async def test_invalid_registration_password_can_be_retried(self) -> None:
        reader, writer, username_response = await self.submit_username(
            "register", "Alice", "register-username"
        )
        self.assertEqual(username_response["action"], "auth_username")

        invalid_response = await self.submit_password(
            reader, writer, "short", "register-password-1"
        )
        self.assertEqual(invalid_response["code"], "invalid_password")

        successful_retry = await self.submit_password(
            reader, writer, PASSWORD, "register-password-2"
        )
        self.assertEqual(successful_retry["type"], "response")
        self.assertEqual(successful_retry["action"], "register")
        await read_packet(reader)
        await self.disconnect(writer)

    async def test_wrong_login_password_can_be_retried(self) -> None:
        reader, writer = await self.authenticate("register", "Alice", "register")
        await read_packet(reader)
        await self.disconnect(writer)

        login_reader, login_writer, username_response = await self.submit_username(
            "login", "ALICE", "login-username"
        )
        self.assertEqual(username_response["action"], "auth_username")

        invalid_response = await self.submit_password(
            login_reader, login_writer, "wrong-password", "login-password-1"
        )
        self.assertEqual(invalid_response["code"], "invalid_credentials")

        successful_retry = await self.submit_password(
            login_reader, login_writer, PASSWORD, "login-password-2"
        )
        self.assertEqual(successful_retry["type"], "response")
        self.assertEqual(successful_retry["action"], "login")
        await read_packet(login_reader)
        await self.disconnect(login_writer)

    async def test_password_is_stored_as_hash(self) -> None:
        reader, writer = await self.authenticate("register", "Alice", "register")
        await read_packet(reader)

        user = chat_server.DATABASE.get_user("alice")
        self.assertIsNotNone(user)
        self.assertNotEqual(user.password_hash, PASSWORD)
        self.assertTrue(verify_password(PASSWORD, user.password_hash))
        await self.disconnect(writer)

    async def test_chat_commands_require_authentication(self) -> None:
        reader, writer = await self.open_connection()
        await write_packet(
            writer,
            {"type": "chat_message", "request_id": "msg-1", "text": "hello"},
        )
        response = await read_packet(reader)
        self.assertEqual(response["type"], "error")
        self.assertEqual(response["code"], "expected_username")
        writer.close()
        await writer.wait_closed()


class PasswordPromptTest(unittest.TestCase):
    @patch(
        "client.client.getpass.getpass",
        side_effect=["first-password", "different", "final-password", "final-password"],
    )
    def test_registration_password_must_be_entered_twice(self, mocked_getpass) -> None:
        self.assertEqual(prompt_password("register"), "final-password")
        self.assertEqual(mocked_getpass.call_count, 4)


if __name__ == "__main__":
    unittest.main()
