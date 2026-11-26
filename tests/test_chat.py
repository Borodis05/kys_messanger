import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from client.client import ChatInterface, build_outgoing_packet, prompt_password
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

    async def test_private_message_is_delivered_only_to_recipient(self) -> None:
        alice_reader, alice_writer = await self.authenticate(
            "register", "Alice", "alice"
        )
        await read_packet(alice_reader)
        bob_reader, bob_writer = await self.authenticate("register", "Bob", "bob")
        await read_packet(alice_reader)
        await read_packet(bob_reader)

        await write_packet(
            alice_writer,
            {
                "type": "private_message",
                "request_id": "private-1",
                "recipient": "Bob",
                "text": "secret",
            },
        )
        acknowledgement = await read_packet(alice_reader)
        alice_copy = await read_packet(alice_reader)
        bob_message = await read_packet(bob_reader)

        self.assertEqual(acknowledgement["action"], "private_message")
        self.assertEqual(alice_copy["text"], "secret")
        self.assertEqual(bob_message["sender"], "Alice")
        self.assertEqual(bob_message["recipient"], "Bob")

        alice_writer.close()
        bob_writer.close()
        await alice_writer.wait_closed()
        await bob_writer.wait_closed()

    async def test_channel_membership_controls_delivery(self) -> None:
        alice_reader, alice_writer = await self.authenticate(
            "register", "Alice", "alice"
        )
        await read_packet(alice_reader)
        bob_reader, bob_writer = await self.authenticate("register", "Bob", "bob")
        await read_packet(alice_reader)
        await read_packet(bob_reader)

        await write_packet(
            alice_writer,
            {
                "type": "create_channel",
                "request_id": "create-1",
                "channel": "Developers",
            },
        )
        created = await read_packet(alice_reader)
        self.assertEqual(created["action"], "create_channel")

        await write_packet(
            bob_writer,
            {
                "type": "list_channels",
                "request_id": "channels-1",
            },
        )
        channels = await read_packet(bob_reader)
        self.assertEqual(channels["channels"][0]["name"], "Developers")
        self.assertFalse(channels["channels"][0]["is_member"])

        await write_packet(
            bob_writer,
            {
                "type": "channel_message",
                "request_id": "channel-denied",
                "channel": "developers",
                "text": "not yet",
            },
        )
        denied = await read_packet(bob_reader)
        self.assertEqual(denied["code"], "not_member")

        await write_packet(
            bob_writer,
            {
                "type": "join_channel",
                "request_id": "join-1",
                "channel": "developers",
            },
        )
        joined = await read_packet(bob_reader)
        self.assertEqual(joined["action"], "join_channel")

        await write_packet(
            alice_writer,
            {
                "type": "channel_message",
                "request_id": "channel-1",
                "channel": "Developers",
                "text": "team message",
            },
        )
        self.assertEqual((await read_packet(alice_reader))["action"], "channel_message")
        self.assertEqual((await read_packet(alice_reader))["text"], "team message")
        self.assertEqual((await read_packet(bob_reader))["text"], "team message")

        await write_packet(
            bob_writer,
            {
                "type": "leave_channel",
                "request_id": "leave-1",
                "channel": "Developers",
            },
        )
        left = await read_packet(bob_reader)
        self.assertEqual(left["action"], "leave_channel")

        alice_writer.close()
        bob_writer.close()
        await alice_writer.wait_closed()
        await bob_writer.wait_closed()

    async def test_chat_list_contains_general_private_and_channel_chats(self) -> None:
        alice_reader, alice_writer = await self.authenticate(
            "register", "Alice", "alice"
        )
        await read_packet(alice_reader)
        bob_reader, bob_writer = await self.authenticate("register", "Bob", "bob")
        await read_packet(alice_reader)
        await read_packet(bob_reader)

        await write_packet(
            alice_writer,
            {
                "type": "create_channel",
                "request_id": "create-1",
                "channel": "Developers",
            },
        )
        await read_packet(alice_reader)

        await write_packet(
            bob_writer,
            {"type": "list_chats", "request_id": "chats-1"},
        )
        response = await read_packet(bob_reader)
        chats = response["chats"]

        self.assertEqual(chats[0]["type"], "general")
        self.assertTrue(chats[0]["is_member"])
        self.assertTrue(
            any(chat["type"] == "private" and chat["title"] == "Alice" for chat in chats)
        )
        developers = next(chat for chat in chats if chat["title"] == "Developers")
        self.assertEqual(developers["type"], "channel")
        self.assertFalse(developers["is_member"])

        alice_writer.close()
        bob_writer.close()
        await alice_writer.wait_closed()
        await bob_writer.wait_closed()

    async def test_history_survives_reconnect_for_all_chat_types(self) -> None:
        alice_reader, alice_writer = await self.authenticate(
            "register", "Alice", "alice"
        )
        await read_packet(alice_reader)
        bob_reader, bob_writer = await self.authenticate("register", "Bob", "bob")
        await read_packet(alice_reader)
        await read_packet(bob_reader)

        await write_packet(
            alice_writer,
            {"type": "chat_message", "request_id": "general-1", "text": "general"},
        )
        await read_packet(alice_reader)
        await read_packet(alice_reader)
        await read_packet(bob_reader)

        await write_packet(
            alice_writer,
            {
                "type": "private_message",
                "request_id": "private-1",
                "recipient": "Bob",
                "text": "private",
            },
        )
        await read_packet(alice_reader)
        await read_packet(alice_reader)
        await read_packet(bob_reader)

        await write_packet(
            alice_writer,
            {
                "type": "create_channel",
                "request_id": "create-1",
                "channel": "Developers",
            },
        )
        await read_packet(alice_reader)
        await write_packet(
            alice_writer,
            {
                "type": "channel_message",
                "request_id": "channel-1",
                "channel": "Developers",
                "text": "channel",
            },
        )
        await read_packet(alice_reader)
        await read_packet(alice_reader)

        alice_writer.close()
        bob_writer.close()
        await alice_writer.wait_closed()
        await bob_writer.wait_closed()
        await self.wait_until_offline()

        history_reader, history_writer = await self.authenticate(
            "login", "Alice", "login"
        )
        await read_packet(history_reader)

        for chat_type, target, expected_text in (
            ("general", "general", "general"),
            ("private", "bob", "private"),
            ("channel", "developers", "channel"),
        ):
            await write_packet(
                history_writer,
                {
                    "type": "get_history",
                    "request_id": f"history-{chat_type}",
                    "chat_type": chat_type,
                    "target": target,
                },
            )
            response = await read_packet(history_reader)
            self.assertEqual(response["action"], "get_history")
            self.assertEqual(response["messages"][-1]["text"], expected_text)

        await write_packet(
            history_writer,
            {"type": "list_chats", "request_id": "chats-after-reconnect"},
        )
        chats = (await read_packet(history_reader))["chats"]
        self.assertTrue(
            any(chat["type"] == "private" and chat["title"] == "Bob" for chat in chats)
        )
        await self.disconnect(history_writer)

    async def test_history_is_limited_to_last_fifty_messages(self) -> None:
        reader, writer = await self.authenticate("register", "Alice", "alice")
        await read_packet(reader)

        def save_messages() -> None:
            for index in range(55):
                chat_server.DATABASE.save_message(
                    "general",
                    "alice",
                    f"message-{index}",
                    f"2026-01-01T00:{index:02d}:00+00:00",
                )

        await asyncio.to_thread(save_messages)

        await write_packet(
            writer,
            {
                "type": "get_history",
                "request_id": "history-limit",
                "chat_type": "general",
                "target": "general",
            },
        )
        messages = (await read_packet(reader))["messages"]
        self.assertEqual(len(messages), 50)
        self.assertEqual(messages[0]["text"], "message-5")
        self.assertEqual(messages[-1]["text"], "message-54")
        await self.disconnect(writer)

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


class CommandParserTest(unittest.TestCase):
    def test_private_message_command(self) -> None:
        packet = build_outgoing_packet("/msg Bob hello there")
        self.assertEqual(packet["type"], "private_message")
        self.assertEqual(packet["recipient"], "Bob")
        self.assertEqual(packet["text"], "hello there")

    def test_channel_message_command(self) -> None:
        packet = build_outgoing_packet("/channel Developers release ready")
        self.assertEqual(packet["type"], "channel_message")
        self.assertEqual(packet["channel"], "Developers")
        self.assertEqual(packet["text"], "release ready")

    def test_numbered_chat_can_be_opened(self) -> None:
        interface = ChatInterface()
        interface.update_chats(
            [
                {
                    "type": "general",
                    "id": "general",
                    "title": "Общий чат",
                    "is_member": True,
                }
            ]
        )
        selected = interface.open_by_number(1)
        self.assertEqual(selected.chat_type, "general")
        self.assertEqual(selected.title, "Общий чат")


if __name__ == "__main__":
    unittest.main()
