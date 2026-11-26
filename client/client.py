import argparse
import asyncio
import getpass
import threading
from typing import Any, Dict

from shared.protocol import (
    MAX_PACKET_SIZE,
    ProtocolError,
    new_request_id,
    read_packet,
    write_packet,
)


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


def display_packet(packet: Dict[str, Any]) -> None:
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
        print(
            f"[Ошибка: {packet.get('code', 'unknown')}] "
            f"{packet.get('message', '')}"
        )
    elif packet_type == "response":
        action = packet.get("action")
        if action == "list_users":
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
            print(f"Канал #{packet.get('channel')} создан.")
        elif action == "join_channel":
            print(f"Вы вступили в канал #{packet.get('channel')}.")
        elif action == "leave_channel":
            print(f"Вы вышли из канала #{packet.get('channel')}.")


def print_help() -> None:
    print("Команды:")
    print("  обычный текст              сообщение в общий чат")
    print("  /users                     пользователи онлайн")
    print("  /msg USER TEXT             личное сообщение")
    print("  /channels                  список каналов")
    print("  /create CHANNEL            создать канал")
    print("  /join CHANNEL              вступить в канал")
    print("  /leave CHANNEL             выйти из канала")
    print("  /channel CHANNEL TEXT      сообщение в канал")
    print("  /exit                      выйти из чата")


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


async def receive_packets(reader: asyncio.StreamReader) -> None:
    while True:
        packet = await read_packet(reader)
        if packet is None:
            return
        display_packet(packet)


async def send_messages(
    writer: asyncio.StreamWriter, messages: asyncio.Queue[str]
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

        try:
            packet = build_outgoing_packet(message)
        except ValueError as error:
            print(f"Ошибка команды: {error} Используйте /help.")
            continue
        await write_packet(writer, packet)


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
        print("Введите сообщение или /help для списка команд.")

        messages: asyncio.Queue[str] = asyncio.Queue()
        threading.Thread(
            target=read_console,
            args=(asyncio.get_running_loop(), messages),
            daemon=True,
        ).start()

        receiver = asyncio.create_task(receive_packets(reader))
        sender = asyncio.create_task(send_messages(writer, messages))
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
