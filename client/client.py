import argparse
import asyncio
import threading


def read_console(
    loop: asyncio.AbstractEventLoop, messages: asyncio.Queue[str]
) -> None:
    """Read blocking console input without blocking the network event loop."""
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


async def receive_messages(reader: asyncio.StreamReader) -> None:
    while raw_message := await reader.readline():
        print(raw_message.decode("utf-8").rstrip("\r\n"))


async def send_messages(
    writer: asyncio.StreamWriter, messages: asyncio.Queue[str]
) -> None:
    while True:
        message = await messages.get()
        if message == "/exit":
            return
        if not message:
            continue

        writer.write(f"{message}\n".encode("utf-8"))
        await writer.drain()


async def run_client(host: str, port: int, nickname: str) -> None:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=10
        )
    except ConnectionRefusedError:
        print(f"Не удалось подключиться: сервер {host}:{port} не запущен.")
        return
    except (TimeoutError, OSError) as error:
        print(f"Ошибка соединения: {error}")
        return

    try:
        writer.write(f"{nickname}\n".encode("utf-8"))
        await writer.drain()

        raw_response = await reader.readline()
        response = raw_response.decode("utf-8").rstrip("\r\n")
        if response != "OK":
            print(response or "Сервер закрыл соединение при входе.")
            return

        print(f"Вы подключены к {host}:{port} как {nickname}.")
        print("Введите сообщение или /exit для выхода.")

        messages: asyncio.Queue[str] = asyncio.Queue()
        console_thread = threading.Thread(
            target=read_console,
            args=(asyncio.get_running_loop(), messages),
            daemon=True,
        )
        console_thread.start()

        receiver = asyncio.create_task(receive_messages(reader))
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
    except (ConnectionError, UnicodeError, OSError) as error:
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
