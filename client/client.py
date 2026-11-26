import argparse
import asyncio
import threading
from typing import Any, Dict

from shared.protocol import (
    MAX_PACKET_SIZE,
    ProtocolError,
    new_request_id,
    read_packet,
    write_packet,
)


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
    elif packet_type == "system":
        print(f"[Система] {packet.get('text', '')}")
    elif packet_type == "error":
        print(
            f"[Ошибка: {packet.get('code', 'unknown')}] "
            f"{packet.get('message', '')}"
        )


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

        await write_packet(
            writer,
            {
                "type": "chat_message",
                "request_id": new_request_id(),
                "text": message,
            },
        )


async def join_chat(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, nickname: str
) -> None:
    request_id = new_request_id()
    await write_packet(
        writer,
        {"type": "join", "request_id": request_id, "name": nickname},
    )
    response = await read_packet(reader)
    if response is None:
        raise ProtocolError("connection_closed", "Сервер закрыл соединение при входе.")
    if response.get("type") == "error":
        raise ProtocolError(
            str(response.get("code", "join_failed")),
            str(response.get("message", "Не удалось войти в чат.")),
        )
    if (
        response.get("type") != "response"
        or response.get("ok") is not True
        or response.get("request_id") != request_id
    ):
        raise ProtocolError("invalid_response", "Сервер прислал некорректный ответ.")


async def run_client(host: str, port: int, nickname: str) -> None:
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
        await join_chat(reader, writer, nickname)
        print(f"Вы подключены к {host}:{port} как {nickname}.")
        print("Введите сообщение или /exit для выхода.")

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
    parser.add_argument("--name", required=True, help="Имя пользователя в чате")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_arguments()
    asyncio.run(run_client(arguments.host, arguments.port, arguments.name))
