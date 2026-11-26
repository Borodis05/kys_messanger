import argparse
import asyncio
import getpass
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from shared.protocol import (
    MAX_PACKET_SIZE,
    ProtocolError,
    new_request_id,
    read_packet,
    write_packet,
)


@dataclass
class ChatTarget:
    chat_type: str
    identifier: str
    title: str
    is_member: bool = True


@dataclass
class ChatInterface:
    chats: list[ChatTarget] = field(default_factory=list)
    current: Optional[ChatTarget] = None
    pending_open: Dict[str, ChatTarget] = field(default_factory=dict)

    def update_chats(self, items: list[Dict[str, Any]]) -> None:
        self.chats = [
            ChatTarget(
                chat_type=str(item.get("type", "")),
                identifier=str(item.get("id", "")),
                title=str(item.get("title", "")),
                is_member=bool(item.get("is_member", False)),
            )
            for item in items
            if item.get("type") in {"general", "private", "channel"}
        ]

    def show_chat_list(self) -> None:
        print("\nВаши чаты:")
        for index, chat in enumerate(self.chats, start=1):
            if chat.chat_type == "general":
                marker = "общий"
            elif chat.chat_type == "private":
                marker = "личный"
            elif chat.is_member:
                marker = "канал"
            else:
                marker = "доступный канал — вступление при открытии"
            print(f"  {index}. {chat.title} ({marker})")
        print("Команды: /open N, /create NAME, /refresh, /help, /exit")

    def open_by_number(self, number: int) -> ChatTarget:
        if number < 1 or number > len(self.chats):
            raise ValueError("Чата с таким номером нет.")
        return self.chats[number - 1]


def prompt_password(action: str) -> str:
    while True:
        password = getpass.getpass("Пароль: ")
        if action != "register":
            return password

        confirmation = getpass.getpass("Повторите пароль: ")
        if password == confirmation:
            return password
        print("Пароли не совпадают. Попробуйте ещё раз.")


def prompt_username() -> str:
    return input("Имя пользователя: ").strip()


def read_console(
    loop: asyncio.AbstractEventLoop, messages: asyncio.Queue[str]
) -> None:
    while True:
        try:
            message = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            message = "/exit"

        try:
            loop.call_soon_threadsafe(messages.put_nowait, message)
        except RuntimeError:
            return

        if message == "/exit":
            return


def display_packet(packet: Dict[str, Any], interface: ChatInterface) -> None:
    packet_type = packet.get("type")
    if packet_type == "message":
        print(f"[{packet.get('sender', '?')}] {packet.get('text', '')}")
    elif packet_type == "private_message":
        print(
            f"[Личное: {packet.get('sender', '?')} → "
            f"{packet.get('recipient', '?')}] {packet.get('text', '')}"
        )
    elif packet_type == "channel_message":
        print(
            f"[#{packet.get('channel', '?')}] "
            f"{packet.get('sender', '?')}: {packet.get('text', '')}"
        )
    elif packet_type == "system":
        print(f"[Система] {packet.get('text', '')}")
    elif packet_type == "error":
        request_id = packet.get("request_id")
        if isinstance(request_id, str):
            interface.pending_open.pop(request_id, None)
        print(
            f"[Ошибка: {packet.get('code', 'unknown')}] "
            f"{packet.get('message', '')}"
        )
    elif packet_type == "response":
        action = packet.get("action")
        if action == "list_chats":
            chats = packet.get("chats", [])
            if isinstance(chats, list):
                interface.update_chats(chats)
                interface.show_chat_list()
        elif action == "list_users":
            users = packet.get("users", [])
            print("Пользователи онлайн: " + (", ".join(users) or "нет"))
        elif action == "list_channels":
            channels = packet.get("channels", [])
            if not channels:
                print("Каналы ещё не созданы.")
            for channel in channels:
                membership = "участник" if channel.get("is_member") else "не участник"
                print(
                    f"#{channel.get('name')} — {membership}, "
                    f"участников: {channel.get('member_count', 0)}"
                )
        elif action == "create_channel":
            channel = str(packet.get("channel", ""))
            interface.current = ChatTarget(
                "channel", channel.casefold(), channel, True
            )
            print(f"Канал #{channel} создан и открыт. /back — к списку чатов.")
        elif action == "join_channel":
            request_id = packet.get("request_id")
            target = (
                interface.pending_open.pop(request_id, None)
                if isinstance(request_id, str)
                else None
            )
            if target is not None:
                target.is_member = True
                interface.current = target
                print(f"Открыт канал #{target.title}. /back — к списку чатов.")
            else:
                print(f"Вы вступили в канал #{packet.get('channel')}.")
        elif action == "leave_channel":
            print(f"Вы вышли из канала #{packet.get('channel')}.")


def print_help() -> None:
    print("Команды списка чатов:")
    print("  /open N                    открыть чат по номеру")
    print("  /create NAME               создать и открыть канал")
    print("  /refresh                   обновить список")
    print("Команды внутри открытого чата:")
    print("  обычный текст              отправить сообщение в текущий чат")
    print("  /leave                     выйти из текущего канала")
    print("  /back                      вернуться к списку чатов")
    print("Общие команды: /help, /exit")


def build_outgoing_packet(message: str) -> Dict[str, Any]:
    request_id = new_request_id()
    if not message.startswith("/"):
        return {"type": "chat_message", "request_id": request_id, "text": message}

    command = message.split(maxsplit=2)
    name = command[0].casefold()
    if name == "/users" and len(command) == 1:
        return {"type": "list_users", "request_id": request_id}
    if name == "/channels" and len(command) == 1:
        return {"type": "list_channels", "request_id": request_id}
    if name in {"/create", "/join", "/leave"} and len(command) == 2:
        packet_types = {
            "/create": "create_channel",
            "/join": "join_channel",
            "/leave": "leave_channel",
        }
        return {
            "type": packet_types[name],
            "request_id": request_id,
            "channel": command[1],
        }
    if name == "/msg" and len(command) == 3:
        return {
            "type": "private_message",
            "request_id": request_id,
            "recipient": command[1],
            "text": command[2],
        }
    if name == "/channel" and len(command) == 3:
        return {
            "type": "channel_message",
            "request_id": request_id,
            "channel": command[1],
            "text": command[2],
        }
    raise ValueError("Неизвестная команда или неверное количество аргументов.")


async def receive_packets(
    reader: asyncio.StreamReader, interface: ChatInterface
) -> None:
    while True:
        packet = await read_packet(reader)
        if packet is None:
            return
        display_packet(packet, interface)


async def request_chat_list(writer: asyncio.StreamWriter) -> None:
    await write_packet(
        writer,
        {"type": "list_chats", "request_id": new_request_id()},
    )


async def send_messages(
    writer: asyncio.StreamWriter,
    messages: asyncio.Queue[str],
    interface: ChatInterface,
) -> None:
    while True:
        message = await messages.get()
        if message == "/exit":
            return
        if not message:
            continue

        if message.casefold() == "/help":
            print_help()
            continue

        if message.casefold() in {"/back", "/chats"}:
            interface.current = None
            await request_chat_list(writer)
            continue

        if message.casefold() == "/refresh":
            await request_chat_list(writer)
            continue

        if message.casefold().startswith("/open "):
            try:
                number = int(message.split(maxsplit=1)[1])
                target = interface.open_by_number(number)
            except (ValueError, IndexError) as error:
                print(f"Не удалось открыть чат: {error}")
                continue

            if target.chat_type == "channel" and not target.is_member:
                request_id = new_request_id()
                interface.pending_open[request_id] = target
                await write_packet(
                    writer,
                    {
                        "type": "join_channel",
                        "request_id": request_id,
                        "channel": target.identifier,
                    },
                )
                print(f"Вступаем в канал #{target.title}...")
            else:
                interface.current = target
                print(f"Открыт чат «{target.title}». /back — к списку чатов.")
            continue

        if message.casefold().startswith("/create "):
            channel = message.split(maxsplit=1)[1].strip()
            if not channel:
                print("Укажите название канала: /create NAME")
                continue
            await write_packet(
                writer,
                {
                    "type": "create_channel",
                    "request_id": new_request_id(),
                    "channel": channel,
                },
            )
            continue

        if message.casefold() == "/leave":
            if interface.current is None or interface.current.chat_type != "channel":
                print("Команда /leave доступна только внутри канала.")
                continue
            await write_packet(
                writer,
                {
                    "type": "leave_channel",
                    "request_id": new_request_id(),
                    "channel": interface.current.identifier,
                },
            )
            interface.current = None
            await request_chat_list(writer)
            continue

        if interface.current is None:
            print("Сначала откройте чат командой /open N или создайте /create NAME.")
            continue

        if message.startswith("/"):
            print("Неизвестная команда. Используйте /help.")
            continue

        request_id = new_request_id()
        if interface.current.chat_type == "general":
            packet = {
                "type": "chat_message",
                "request_id": request_id,
                "text": message,
            }
        elif interface.current.chat_type == "private":
            packet = {
                "type": "private_message",
                "request_id": request_id,
                "recipient": interface.current.identifier,
                "text": message,
            }
        else:
            packet = {
                "type": "channel_message",
                "request_id": request_id,
                "channel": interface.current.identifier,
                "text": message,
            }
        await write_packet(writer, packet)
        continue



async def authenticate(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    action: str,
) -> str:
    while True:
        username = await asyncio.to_thread(prompt_username)
        username_request_id = new_request_id()
        await write_packet(
            writer,
            {
                "type": "auth_username",
                "action": action,
                "request_id": username_request_id,
                "username": username,
            },
        )
        username_response = await read_packet(reader)
        if username_response is None:
            raise ProtocolError(
                "connection_closed", "Сервер закрыл соединение при аутентификации."
            )
        if username_response.get("type") == "error":
            print(
                f"Ошибка имени [{username_response.get('code', 'auth_failed')}]: "
                f"{username_response.get('message', '')}"
            )
            continue
        if (
            username_response.get("type") != "response"
            or username_response.get("ok") is not True
            or username_response.get("request_id") != username_request_id
            or username_response.get("action") != "auth_username"
        ):
            raise ProtocolError(
                "invalid_response", "Сервер прислал некорректный ответ."
            )

        authenticated_name = username_response.get("username")
        if not isinstance(authenticated_name, str):
            raise ProtocolError(
                "invalid_response", "В ответе сервера отсутствует username."
            )
        print(f"Имя {authenticated_name} принято.")

        while True:
            password = await asyncio.to_thread(prompt_password, action)
            password_request_id = new_request_id()
            await write_packet(
                writer,
                {
                    "type": "auth_password",
                    "request_id": password_request_id,
                    "password": password,
                },
            )
            password_response = await read_packet(reader)
            if password_response is None:
                raise ProtocolError(
                    "connection_closed",
                    "Сервер закрыл соединение при аутентификации.",
                )
            if password_response.get("type") == "error":
                error_code = str(password_response.get("code", "auth_failed"))
                print(
                    f"Ошибка пароля [{error_code}]: "
                    f"{password_response.get('message', '')}"
                )
                if error_code in {"user_exists", "expected_username"}:
                    break
                continue
            if (
                password_response.get("type") != "response"
                or password_response.get("ok") is not True
                or password_response.get("request_id") != password_request_id
                or password_response.get("action") != action
            ):
                raise ProtocolError(
                    "invalid_response", "Сервер прислал некорректный ответ."
                )
            return authenticated_name


async def run_client(host: str, port: int, action: str) -> None:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, limit=MAX_PACKET_SIZE + 1),
            timeout=10,
        )
    except ConnectionRefusedError:
        print(f"Не удалось подключиться: сервер {host}:{port} не запущен.")
        return
    except (TimeoutError, OSError) as error:
        print(f"Ошибка соединения: {error}")
        return

    try:
        authenticated_name = await authenticate(reader, writer, action)
        print(f"Вы подключены к {host}:{port} как {authenticated_name}.")
        print("Загружаем список чатов...")

        messages: asyncio.Queue[str] = asyncio.Queue()
        interface = ChatInterface()
        threading.Thread(
            target=read_console,
            args=(asyncio.get_running_loop(), messages),
            daemon=True,
        ).start()

        await request_chat_list(writer)
        receiver = asyncio.create_task(receive_packets(reader, interface))
        sender = asyncio.create_task(send_messages(writer, messages, interface))
        done, pending = await asyncio.wait(
            {receiver, sender}, return_when=asyncio.FIRST_COMPLETED
        )

        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        for task in done:
            if task.exception():
                raise task.exception()
    except ProtocolError as error:
        print(f"Ошибка протокола [{error.code}]: {error.message}")
    except (ConnectionError, OSError) as error:
        print(f"Ошибка соединения: {error}")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        print("Соединение закрыто.")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Консольный TCP-клиент чата")
    parser.add_argument("--host", default="127.0.0.1", help="Адрес сервера")
    parser.add_argument("--port", type=int, default=8888, help="TCP-порт сервера")
    commands = parser.add_subparsers(dest="action", required=True)

    register_parser = commands.add_parser("register", help="Создать учётную запись")

    login_parser = commands.add_parser("login", help="Войти в существующую запись")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_arguments()
    try:
        asyncio.run(run_client(arguments.host, arguments.port, arguments.action))
    except KeyboardInterrupt:
        print("\nКлиент остановлен.")
