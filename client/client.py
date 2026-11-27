import argparse
import asyncio
import getpass
import json
import ssl
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set, Tuple

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import clear as clear_terminal

from shared.protocol import (
    MAX_PACKET_SIZE,
    ProtocolError,
    new_request_id,
    read_packet,
    write_packet,
)
from shared.tls import TLSConfigurationError, create_client_tls_context
from shared.runtime import application_directory, runtime_path


@dataclass
class ChatTarget:
    chat_type: str
    identifier: str
    title: str
    is_member: bool = True


@dataclass
class ChatInterface:
    own_display_name: str = ""
    chats: list[ChatTarget] = field(default_factory=list)
    current: Optional[ChatTarget] = None
    pending_open: Dict[str, ChatTarget] = field(default_factory=dict)
    unread: Set[Tuple[str, str]] = field(default_factory=set)
    message_lines: list[str] = field(default_factory=list)

    def update_chats(self, items: list[Dict[str, Any]]) -> bool:
        previous_unread = set(self.unread)
        updated_chats = [
            ChatTarget(
                chat_type=str(item.get("type", "")),
                identifier=str(item.get("id", "")),
                title=str(item.get("title", "")),
                is_member=bool(item.get("is_member", False)),
            )
            for item in items
            if item.get("type") in {"general", "private", "channel"}
        ]
        changed = updated_chats != self.chats
        self.chats = updated_chats
        for item, chat in zip(items, updated_chats):
            if int(item.get("unread_count", 0) or 0) > 0:
                self.unread.add(self.chat_key(chat))
        return changed or self.unread != previous_unread

    @staticmethod
    def chat_key(chat: ChatTarget) -> Tuple[str, str]:
        return chat.chat_type, chat.identifier.casefold()

    def render_chat_list(self) -> None:
        clear_screen()
        print("КОРПОРАТИВНЫЙ ЧАТ — СПИСОК ДИАЛОГОВ")
        number_width = 3
        title_width = 28
        type_width = 18
        unread_width = 5
        separator = (
            f"+-{'-' * number_width}-+-{'-' * title_width}-+"
            f"-{'-' * type_width}-+-{'-' * unread_width}-+"
        )
        print(separator)
        print(
            f"| {'#':^{number_width}} | {'Чат':<{title_width}} | "
            f"{'Тип':<{type_width}} | {'Новое':<{unread_width}} |"
        )
        print(separator)
        for index, chat in enumerate(self.chats, start=1):
            if chat.chat_type == "general":
                chat_type = "Общий"
            elif chat.chat_type == "private":
                chat_type = "Личный"
            elif chat.is_member:
                chat_type = "Канал"
            else:
                chat_type = "Доступный канал"
            unread = "!" if self.chat_key(chat) in self.unread else ""
            print(
                f"| {index:>{number_width}} | "
                f"{truncate(chat.title, title_width):<{title_width}} | "
                f"{chat_type:<{type_width}} | {unread:^{unread_width}} |"
            )
        print(separator)

    def render_current_chat(self, loading: bool = False) -> None:
        if self.current is None:
            return
        clear_screen()
        title = truncate(self.current.title, 54)
        print("╔" + "═" * 58 + "╗")
        print(f"║ {title:^56} ║")
        print("╚" + "═" * 58 + "╝")
        print("─" * 60)
        if loading:
            print("Загрузка истории...")
        else:
            for line in self.message_lines:
                print(line)

    def select_chat(self, chat: ChatTarget) -> None:
        self.current = chat
        self.unread.discard(self.chat_key(chat))
        self.message_lines.clear()
        self.render_current_chat(loading=True)

    def open_by_number(self, number: int) -> ChatTarget:
        if number < 1 or number > len(self.chats):
            raise ValueError("Чата с таким номером нет.")
        return self.chats[number - 1]


def clear_screen() -> None:
    try:
        clear_terminal()
    except Exception:
        # IDE output panes and redirected streams may not expose a console buffer.
        print("\n" * 3, end="")


def truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


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


def bottom_toolbar(interface: ChatInterface) -> str:
    if interface.current is None:
        return " /open N   /new USER   /create NAME   /help   /exit "
    return " /back   /history   /help   /exit "


async def read_console(
    messages: asyncio.Queue[str], interface: ChatInterface
) -> None:
    session: PromptSession[str] = PromptSession()
    while True:
        try:
            with patch_stdout():
                message = (
                    await session.prompt_async(
                        "> ",
                        bottom_toolbar=lambda: bottom_toolbar(interface),
                    )
                ).strip()
        except (EOFError, KeyboardInterrupt):
            message = "/exit"

        await messages.put(message)
        if message == "/exit":
            return


def packet_chat_key(
    packet: Dict[str, Any], interface: ChatInterface
) -> Optional[Tuple[str, str]]:
    packet_type = packet.get("type")
    if packet_type == "message":
        return "general", "general"
    if packet_type == "channel_message":
        channel = packet.get("channel_id", packet.get("channel", ""))
        return "channel", str(channel).casefold()
    if packet_type == "private_message":
        sender = str(packet.get("sender_id", ""))
        recipient = str(packet.get("recipient_id", ""))
        own_name = interface.own_display_name.casefold()
        if str(packet.get("sender", "")).casefold() == own_name:
            contact = recipient
        else:
            contact = sender
        return "private", contact.casefold()
    return None


def format_live_message(packet: Dict[str, Any]) -> str:
    return format_message_line(
        str(packet.get("sent_at", "")),
        str(packet.get("sender", "?")),
        str(packet.get("text", "")),
    )


def format_message_line(timestamp: str, sender: str, text: str) -> str:
    if len(timestamp) >= 16:
        displayed_time = f"{timestamp[8:10]}.{timestamp[5:7]} {timestamp[11:16]}"
    else:
        displayed_time = timestamp
    return f"[{displayed_time}] {sender}: {text}"


def display_packet(packet: Dict[str, Any], interface: ChatInterface) -> None:
    packet_type = packet.get("type")
    if packet_type in {"message", "private_message", "channel_message"}:
        chat_key = packet_chat_key(packet, interface)
        current_key = (
            interface.chat_key(interface.current) if interface.current else None
        )
        if chat_key == current_key:
            interface.message_lines.append(format_live_message(packet))
            interface.message_lines = interface.message_lines[-50:]
            interface.render_current_chat()
        elif chat_key is not None:
            interface.unread.add(chat_key)
            if interface.current is None:
                interface.render_chat_list()
        return
    if packet_type == "system":
        return
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
                changed = interface.update_chats(chats)
                if interface.current is None and changed:
                    interface.render_chat_list()
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
            interface.select_chat(
                ChatTarget("channel", channel.casefold(), channel, True)
            )
        elif action == "open_private_chat":
            recipient = str(packet.get("recipient", ""))
            title = str(packet.get("title", recipient))
            interface.select_chat(
                ChatTarget("private", recipient.casefold(), title, True)
            )
        elif action == "join_channel":
            request_id = packet.get("request_id")
            target = (
                interface.pending_open.pop(request_id, None)
                if isinstance(request_id, str)
                else None
            )
            if target is not None:
                target.is_member = True
                interface.select_chat(target)
            else:
                print(f"Вы вступили в канал #{packet.get('channel')}.")
        elif action == "leave_channel":
            print(f"Вы вышли из канала #{packet.get('channel')}.")
        elif action == "get_history":
            title = packet.get("title", "Чат")
            messages = packet.get("messages", [])
            response_key = (
                str(packet.get("chat_type", "")),
                str(packet.get("target", "")).casefold(),
            )
            current_key = (
                interface.chat_key(interface.current) if interface.current else None
            )
            if response_key != current_key:
                return
            history_lines = [f"История: {title}"]
            if not messages:
                history_lines.append("Сообщений пока нет.")
            unread_header_printed = False
            for message in messages:
                if message.get("is_unread") and not unread_header_printed:
                    history_lines.append("── НОВЫЕ СООБЩЕНИЯ ──")
                    unread_header_printed = True
                history_lines.append(
                    format_message_line(
                        str(message.get("sent_at", "")),
                        str(message.get("sender", "?")),
                        str(message.get("text", "")),
                    )
                )
            interface.message_lines = history_lines
            interface.render_current_chat()


def print_help() -> None:
    print("Команды списка чатов:")
    print("  /open N                    открыть чат по номеру")
    print("  /new USER                  начать личный диалог")
    print("  /create NAME               создать и открыть канал")
    print("  /refresh                   обновить список")
    print("Команды внутри открытого чата:")
    print("  обычный текст              отправить сообщение в текущий чат")
    print("  /leave                     выйти из текущего канала")
    print("  /history                   повторно загрузить историю")
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
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    interface: ChatInterface,
) -> None:
    while True:
        packet = await read_packet(reader)
        if packet is None:
            return
        action = packet.get("action") if packet.get("type") == "response" else None
        display_packet(packet, interface)
        if action in {"create_channel", "join_channel", "open_private_chat"} and interface.current:
            await request_history(writer, interface.current)


async def request_chat_list(writer: asyncio.StreamWriter) -> None:
    await write_packet(
        writer,
        {"type": "list_chats", "request_id": new_request_id()},
    )


async def auto_refresh_chat_list(writer: asyncio.StreamWriter) -> None:
    while True:
        await asyncio.sleep(3)
        await request_chat_list(writer)


async def request_history(
    writer: asyncio.StreamWriter, target: ChatTarget
) -> None:
    await write_packet(
        writer,
        {
            "type": "get_history",
            "request_id": new_request_id(),
            "chat_type": target.chat_type,
            "target": target.identifier,
        },
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
            interface.render_chat_list()
            await request_chat_list(writer)
            continue

        if message.casefold() == "/refresh":
            if interface.current is None:
                interface.render_chat_list()
            await request_chat_list(writer)
            continue

        if message.casefold() == "/history":
            if interface.current is None:
                print("Сначала откройте чат командой /open N.")
            else:
                await request_history(writer, interface.current)
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
                interface.select_chat(target)
                await request_history(writer, target)
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

        if message.casefold().startswith("/new "):
            recipient = message.split(maxsplit=1)[1].strip()
            if not recipient:
                print("Укажите пользователя: /new USER")
                continue
            await write_packet(
                writer,
                {
                    "type": "open_private_chat",
                    "request_id": new_request_id(),
                    "recipient": recipient,
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


async def run_client(
    host: str,
    port: int,
    action: str,
    cafile: str,
    server_name: Optional[str],
) -> None:
    try:
        tls_context = create_client_tls_context(cafile)
    except TLSConfigurationError as error:
        print(f"Ошибка TLS: {error}")
        return

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host,
                port,
                limit=MAX_PACKET_SIZE + 1,
                ssl=tls_context,
                server_hostname=server_name or host,
                ssl_handshake_timeout=10,
            ),
            timeout=10,
        )
    except ssl.SSLCertVerificationError as error:
        print(f"Сертификат сервера не прошёл проверку: {error}")
        return
    except ssl.SSLError as error:
        print(f"Не удалось установить защищённое соединение: {error}")
        return
    except ConnectionRefusedError:
        print(f"Не удалось подключиться: сервер {host}:{port} не запущен.")
        return
    except (TimeoutError, OSError) as error:
        print(f"Ошибка соединения: {error}")
        return

    try:
        authenticated_name = await authenticate(reader, writer, action)
        tls_session = writer.get_extra_info("ssl_object")
        tls_version = tls_session.version() if tls_session else "неизвестно"
        print(
            f"Вы подключены к {host}:{port} как {authenticated_name}. "
            f"Защита: {tls_version}."
        )
        print("Загружаем список чатов...")

        messages: asyncio.Queue[str] = asyncio.Queue()
        interface = ChatInterface(own_display_name=authenticated_name)

        await request_chat_list(writer)
        console = asyncio.create_task(read_console(messages, interface))
        receiver = asyncio.create_task(receive_packets(reader, writer, interface))
        sender = asyncio.create_task(send_messages(writer, messages, interface))
        refresher = asyncio.create_task(auto_refresh_chat_list(writer))
        done, pending = await asyncio.wait(
            {console, receiver, sender, refresher},
            return_when=asyncio.FIRST_COMPLETED,
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
    parser.add_argument(
        "--cafile",
        default="certs/ca.crt",
        help="Сертификат доверенного локального CA",
    )
    parser.add_argument(
        "--server-name",
        default=None,
        help="Имя или IP из сертификата сервера (по умолчанию --host)",
    )
    commands = parser.add_subparsers(dest="action")

    register_parser = commands.add_parser("register", help="Создать учётную запись")

    login_parser = commands.add_parser("login", help="Войти в существующую запись")
    return parser.parse_args()


def load_client_settings() -> Dict[str, Any]:
    settings_path = application_directory() / "client-settings.json"
    try:
        with settings_path.open("r", encoding="utf-8") as settings_file:
            settings = json.load(settings_file)
        return settings if isinstance(settings, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_client_settings(host: str, port: int) -> None:
    settings_path = application_directory() / "client-settings.json"
    try:
        with settings_path.open("w", encoding="utf-8") as settings_file:
            json.dump(
                {"host": host, "port": port},
                settings_file,
                ensure_ascii=False,
                indent=2,
            )
    except OSError as error:
        print(f"Не удалось сохранить настройки клиента: {error}")


def configure_interactively(arguments: argparse.Namespace) -> None:
    settings = load_client_settings()
    default_host = str(settings.get("host") or arguments.host)
    try:
        default_port = int(settings.get("port") or arguments.port)
    except (TypeError, ValueError):
        default_port = arguments.port

    print("КОРПОРАТИВНЫЙ ЧАТ")
    print("Первичная настройка подключения")
    entered_host = input(f"Адрес сервера [{default_host}]: ").strip()
    arguments.host = entered_host or default_host

    entered_port = input(f"Порт [{default_port}]: ").strip()
    if entered_port:
        try:
            arguments.port = int(entered_port)
        except ValueError:
            print(f"Некорректный порт, используется {default_port}.")
            arguments.port = default_port
    else:
        arguments.port = default_port

    while True:
        choice = input("1 — войти, 2 — зарегистрироваться: ").strip()
        if choice == "1":
            arguments.action = "login"
            break
        if choice == "2":
            arguments.action = "register"
            break
        print("Введите 1 или 2.")

    save_client_settings(arguments.host, arguments.port)


if __name__ == "__main__":
    arguments = parse_arguments()
    interactive_launch = arguments.action is None
    try:
        if interactive_launch:
            configure_interactively(arguments)
        asyncio.run(
            run_client(
                arguments.host,
                arguments.port,
                arguments.action,
                runtime_path(arguments.cafile),
                arguments.server_name,
            )
        )
    except KeyboardInterrupt:
        print("\nКлиент остановлен.")
    finally:
        if interactive_launch:
            try:
                input("Нажмите Enter, чтобы закрыть окно...")
            except (EOFError, KeyboardInterrupt):
                pass
