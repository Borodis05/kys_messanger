import argparse
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from server.auth import (
    CredentialsError,
    DUMMY_PASSWORD_HASH,
    hash_password,
    validate_password,
    validate_username,
    verify_password,
)
from server.database import Database, User
from shared.protocol import MAX_PACKET_SIZE, ProtocolError, read_packet, write_packet


MAX_MESSAGE_LENGTH = 2000
MAX_REQUEST_ID_LENGTH = 64
CLIENTS: Dict[str, asyncio.StreamWriter] = {}
DATABASE = Database("data/chat.db")


def configure_database(path: str) -> None:
    global DATABASE
    DATABASE = Database(path)
    DATABASE.initialize()


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_request_id(packet: Dict[str, Any]) -> str:
    request_id = packet.get("request_id")
    if (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id) > MAX_REQUEST_ID_LENGTH
    ):
        raise ProtocolError(
            "invalid_request_id",
            "Поле request_id должно быть непустой строкой до 64 символов.",
        )
    return request_id


async def send_error(
    writer: asyncio.StreamWriter,
    error: ProtocolError,
    request_id: Optional[str] = None,
) -> None:
    packet: Dict[str, Any] = {
        "type": "error",
        "ok": False,
        "code": error.code,
        "message": error.message,
    }
    if request_id:
        packet["request_id"] = request_id
    await write_packet(writer, packet)


async def send_success(
    writer: asyncio.StreamWriter,
    request_id: str,
    action: str,
    **fields: Any,
) -> None:
    packet = {
        "type": "response",
        "ok": True,
        "action": action,
        "request_id": request_id,
    }
    packet.update(fields)
    await write_packet(writer, packet)


async def broadcast(packet: Dict[str, Any]) -> None:
    disconnected = []
    for username, writer in list(CLIENTS.items()):
        try:
            await write_packet(writer, packet)
        except (ConnectionError, OSError):
            disconnected.append(username)

    for username in disconnected:
        CLIENTS.pop(username, None)


def parse_username(packet: Dict[str, Any], request_id: str) -> Tuple[str, str]:
    try:
        username, display_name = validate_username(packet.get("username"))
    except CredentialsError as error:
        raise ProtocolError("invalid_username", str(error), request_id) from error
    return username, display_name


def parse_password(packet: Dict[str, Any], request_id: str) -> str:
    try:
        password = validate_password(packet.get("password"))
    except CredentialsError as error:
        raise ProtocolError("invalid_password", str(error), request_id) from error
    return password


async def register_user(
    username: str, display_name: str, password: str, request_id: str
) -> User:
    password_hash = await asyncio.to_thread(hash_password, password)
    user = await asyncio.to_thread(
        DATABASE.create_user, username, display_name, password_hash
    )
    if user is None:
        raise ProtocolError(
            "user_exists", "Пользователь с таким именем уже зарегистрирован.", request_id
        )
    return user


async def login_user(username: str, password: str, request_id: str) -> User:
    user = await asyncio.to_thread(DATABASE.get_user, username)
    stored_hash = user.password_hash if user is not None else DUMMY_PASSWORD_HASH
    password_is_valid = await asyncio.to_thread(
        verify_password, password, stored_hash
    )
    if user is None or not password_is_valid:
        raise ProtocolError(
            "invalid_credentials", "Неверное имя пользователя или пароль.", request_id
        )
    return user


async def authenticate_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> Tuple[str, str, str, str]:
    pending_action: Optional[str] = None
    pending_username: Optional[str] = None
    pending_display_name: Optional[str] = None

    while True:
        try:
            packet = await asyncio.wait_for(read_packet(reader), timeout=300)
            if packet is None:
                raise ProtocolError(
                    "connection_closed", "Соединение закрыто до аутентификации."
                )

            request_id = require_request_id(packet)
            packet_type = packet.get("type")

            if packet_type == "auth_username":
                action = packet.get("action")
                if action not in {"register", "login"}:
                    raise ProtocolError(
                        "invalid_auth_action",
                        "Поле action должно иметь значение register или login.",
                        request_id,
                    )

                username, entered_name = parse_username(packet, request_id)
                existing_user = await asyncio.to_thread(DATABASE.get_user, username)
                if action == "register" and existing_user is not None:
                    raise ProtocolError(
                        "user_exists",
                        "Пользователь с таким именем уже зарегистрирован.",
                        request_id,
                    )
                if action == "login" and existing_user is None:
                    raise ProtocolError(
                        "username_not_found",
                        "Пользователь с таким именем не найден.",
                        request_id,
                    )
                if username in CLIENTS:
                    raise ProtocolError(
                        "already_online",
                        "Эта учётная запись уже используется в чате.",
                        request_id,
                    )

                pending_action = action
                pending_username = username
                pending_display_name = (
                    existing_user.display_name
                    if existing_user is not None
                    else entered_name
                )
                await send_success(
                    writer,
                    request_id,
                    "auth_username",
                    auth_action=action,
                    username=pending_display_name,
                )
                continue

            if packet_type != "auth_password" or pending_action is None:
                raise ProtocolError(
                    "expected_username",
                    "Сначала отправьте пакет auth_username.",
                    request_id,
                )

            password = parse_password(packet, request_id)
            if pending_action == "register":
                try:
                    user = await register_user(
                        pending_username,
                        pending_display_name,
                        password,
                        request_id,
                    )
                except ProtocolError as error:
                    if error.code == "user_exists":
                        pending_action = None
                        pending_username = None
                        pending_display_name = None
                    raise
            else:
                user = await login_user(pending_username, password, request_id)

            return user.username, user.display_name, request_id, pending_action
        except ProtocolError as error:
            if error.code == "connection_closed":
                raise
            await send_error(writer, error, error.request_id)


async def process_client_packet(
    packet: Dict[str, Any], display_name: str, writer: asyncio.StreamWriter
) -> None:
    request_id = require_request_id(packet)
    packet_type = packet.get("type")
    if packet_type != "chat_message":
        raise ProtocolError(
            "unknown_type", f"Неизвестный тип пакета: {packet_type!r}."
        )

    message = packet.get("text")
    if not isinstance(message, str):
        raise ProtocolError("invalid_message", "Поле text должно быть строкой.")

    message = message.strip()
    if not message or len(message) > MAX_MESSAGE_LENGTH:
        raise ProtocolError(
            "invalid_message",
            f"Сообщение должно содержать от 1 до {MAX_MESSAGE_LENGTH} символов.",
        )

    await send_success(writer, request_id, "chat_message")
    print(f"[{display_name}] {message}")
    await broadcast(
        {
            "type": "message",
            "sender": display_name,
            "text": message,
            "sent_at": utc_timestamp(),
        }
    )


async def handle_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    address = writer.get_extra_info("peername")
    username: Optional[str] = None
    display_name: Optional[str] = None
    print(f"[+] Новое подключение: {address}")

    try:
        try:
            username, display_name, auth_request_id, auth_action = (
                await authenticate_client(reader, writer)
            )
        except ProtocolError:
            return

        CLIENTS[username] = writer
        await send_success(
            writer,
            auth_request_id,
            auth_action,
            username=display_name,
        )
        print(f"[+] Пользователь вошёл: {display_name} ({address})")
        await broadcast(
            {
                "type": "system",
                "event": "user_joined",
                "text": f"{display_name} присоединился к чату.",
                "sent_at": utc_timestamp(),
            }
        )

        while True:
            request_id = None
            try:
                packet = await read_packet(reader)
                if packet is None:
                    break
                request_id = packet.get("request_id")
                await process_client_packet(packet, display_name, writer)
            except ProtocolError as error:
                valid_request_id = (
                    request_id
                    if isinstance(request_id, str)
                    and len(request_id) <= MAX_REQUEST_ID_LENGTH
                    else None
                )
                await send_error(writer, error, valid_request_id)
    except asyncio.TimeoutError:
        await send_error(
            writer,
            ProtocolError(
                "auth_timeout", "Время ожидания аутентификации истекло."
            ),
        )
    except (ConnectionError, OSError) as error:
        print(f"[!] Ошибка соединения с {address}: {error}")
    finally:
        if username and CLIENTS.get(username) is writer:
            CLIENTS.pop(username, None)
            print(f"[-] Пользователь вышел: {display_name}")
            await broadcast(
                {
                    "type": "system",
                    "event": "user_left",
                    "text": f"{display_name} покинул чат.",
                    "sent_at": utc_timestamp(),
                }
            )

        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def run_server(host: str, port: int) -> None:
    await asyncio.to_thread(DATABASE.initialize)
    server = await asyncio.start_server(
        handle_client, host, port, limit=MAX_PACKET_SIZE + 1
    )
    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    print(f"Сервер запущен: {addresses}")
    print(f"База данных: {DATABASE.path}")
    print("Ожидание клиентов. Для остановки нажмите Ctrl+C.")

    async with server:
        await server.serve_forever()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Асинхронный TCP-сервер чата")
    parser.add_argument("--host", default="127.0.0.1", help="Адрес для прослушивания")
    parser.add_argument("--port", type=int, default=8888, help="TCP-порт")
    parser.add_argument(
        "--database", default="data/chat.db", help="Путь к базе данных SQLite"
    )
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_arguments()
    DATABASE = Database(arguments.database)
    try:
        asyncio.run(run_server(arguments.host, arguments.port))
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
