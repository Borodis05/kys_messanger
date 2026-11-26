import argparse
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from shared.protocol import MAX_PACKET_SIZE, ProtocolError, read_packet, write_packet


MAX_NAME_LENGTH = 32
MAX_MESSAGE_LENGTH = 2000
MAX_REQUEST_ID_LENGTH = 64
CLIENTS: Dict[str, asyncio.StreamWriter] = {}


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
    writer: asyncio.StreamWriter, request_id: str, action: str
) -> None:
    await write_packet(
        writer,
        {
            "type": "response",
            "ok": True,
            "action": action,
            "request_id": request_id,
        },
    )


async def broadcast(packet: Dict[str, Any]) -> None:
    disconnected = []
    for name, writer in list(CLIENTS.items()):
        try:
            await write_packet(writer, packet)
        except (ConnectionError, OSError):
            disconnected.append(name)

    for name in disconnected:
        CLIENTS.pop(name, None)


async def read_join_request(reader: asyncio.StreamReader) -> tuple[str, str]:
    packet = await asyncio.wait_for(read_packet(reader), timeout=30)
    if packet is None:
        raise ProtocolError("connection_closed", "Соединение закрыто до входа.")

    request_id = require_request_id(packet)
    if packet.get("type") != "join":
        raise ProtocolError(
            "expected_join", "Первый пакет должен иметь тип join.", request_id
        )

    nickname = packet.get("name")
    if not isinstance(nickname, str):
        raise ProtocolError(
            "invalid_name", "Поле name должно быть строкой.", request_id
        )

    nickname = nickname.strip()
    if not nickname or len(nickname) > MAX_NAME_LENGTH:
        raise ProtocolError(
            "invalid_name",
            "Имя должно содержать от 1 до 32 символов.",
            request_id,
        )
    if nickname in CLIENTS:
        raise ProtocolError(
            "name_taken",
            "Пользователь с таким именем уже подключён.",
            request_id,
        )

    return nickname, request_id


async def process_client_packet(
    packet: Dict[str, Any], nickname: str, writer: asyncio.StreamWriter
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
    print(f"[{nickname}] {message}")
    await broadcast(
        {
            "type": "message",
            "sender": nickname,
            "text": message,
            "sent_at": utc_timestamp(),
        }
    )


async def handle_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    address = writer.get_extra_info("peername")
    nickname: Optional[str] = None
    print(f"[+] Новое подключение: {address}")

    try:
        try:
            nickname, join_request_id = await read_join_request(reader)
        except ProtocolError as error:
            await send_error(writer, error, error.request_id)
            return

        CLIENTS[nickname] = writer
        await send_success(writer, join_request_id, "join")
        print(f"[+] Пользователь вошёл: {nickname} ({address})")
        await broadcast(
            {
                "type": "system",
                "event": "user_joined",
                "text": f"{nickname} присоединился к чату.",
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
                await process_client_packet(packet, nickname, writer)
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
            ProtocolError("join_timeout", "Время ожидания входа истекло."),
        )
    except (ConnectionError, OSError) as error:
        print(f"[!] Ошибка соединения с {address}: {error}")
    finally:
        if nickname and CLIENTS.get(nickname) is writer:
            CLIENTS.pop(nickname, None)
            print(f"[-] Пользователь вышел: {nickname}")
            await broadcast(
                {
                    "type": "system",
                    "event": "user_left",
                    "text": f"{nickname} покинул чат.",
                    "sent_at": utc_timestamp(),
                }
            )

        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def run_server(host: str, port: int) -> None:
    server = await asyncio.start_server(
        handle_client, host, port, limit=MAX_PACKET_SIZE + 1
    )
    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    print(f"Сервер запущен: {addresses}")
    print("Ожидание клиентов. Для остановки нажмите Ctrl+C.")

    async with server:
        await server.serve_forever()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Асинхронный TCP-сервер чата")
    parser.add_argument("--host", default="127.0.0.1", help="Адрес для прослушивания")
    parser.add_argument("--port", type=int, default=8888, help="TCP-порт")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_arguments()
    try:
        asyncio.run(run_server(arguments.host, arguments.port))
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
