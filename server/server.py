import argparse
import asyncio
from typing import Optional


MAX_NAME_LENGTH = 32
MAX_MESSAGE_LENGTH = 2000
CLIENTS: dict[str, asyncio.StreamWriter] = {}


async def send_line(writer: asyncio.StreamWriter, text: str) -> None:
    writer.write(f"{text}\n".encode("utf-8"))
    await writer.drain()


async def broadcast(text: str) -> None:
    """Send a message to every currently connected client."""
    disconnected: list[str] = []

    for name, writer in list(CLIENTS.items()):
        try:
            await send_line(writer, text)
        except (ConnectionError, OSError):
            disconnected.append(name)

    for name in disconnected:
        CLIENTS.pop(name, None)


async def handle_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    address = writer.get_extra_info("peername")
    nickname: Optional[str] = None
    print(f"[+] Новое подключение: {address}")

    try:
        raw_name = await asyncio.wait_for(reader.readline(), timeout=30)
        nickname = raw_name.decode("utf-8").strip()

        if not nickname or len(nickname) > MAX_NAME_LENGTH:
            await send_line(writer, "ERROR Имя должно содержать от 1 до 32 символов.")
            return
        if nickname in CLIENTS:
            await send_line(writer, "ERROR Пользователь с таким именем уже подключён.")
            return

        CLIENTS[nickname] = writer
        await send_line(writer, "OK")
        print(f"[+] Пользователь вошёл: {nickname} ({address})")
        await broadcast(f"[Система] {nickname} присоединился к чату.")

        while raw_message := await reader.readline():
            message = raw_message.decode("utf-8").strip()
            if not message:
                continue
            if len(message) > MAX_MESSAGE_LENGTH:
                await send_line(
                    writer,
                    f"[Система] Сообщение длиннее {MAX_MESSAGE_LENGTH} символов.",
                )
                continue

            print(f"[{nickname}] {message}")
            await broadcast(f"[{nickname}] {message}")
    except asyncio.TimeoutError:
        await send_line(writer, "ERROR Время ожидания имени истекло.")
    except (ConnectionError, UnicodeError, OSError) as error:
        print(f"[!] Ошибка соединения с {address}: {error}")
    finally:
        if nickname and CLIENTS.get(nickname) is writer:
            CLIENTS.pop(nickname, None)
            print(f"[-] Пользователь вышел: {nickname}")
            await broadcast(f"[Система] {nickname} покинул чат.")

        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def run_server(host: str, port: int) -> None:
    server = await asyncio.start_server(handle_client, host, port)
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
