import argparse
import socket


def run_client(host: str, port: int) -> None:
    try:
        with socket.create_connection((host, port), timeout=10) as client_socket:
            client_socket.settimeout(None)
            reader = client_socket.makefile("r", encoding="utf-8", newline="\n")

            print(f"Подключение к серверу {host}:{port} установлено.")
            print("Введите сообщение или /exit для выхода.")

            try:
                while True:
                    message = input("> ").strip()

                    if message == "/exit":
                        break
                    if not message:
                        continue

                    client_socket.sendall(f"{message}\n".encode("utf-8"))
                    response = reader.readline()

                    if not response:
                        print("Сервер закрыл соединение.")
                        break

                    print(response.rstrip("\r\n"))
            finally:
                reader.close()

        print("Соединение закрыто.")
    except ConnectionRefusedError:
        print(f"Не удалось подключиться: сервер {host}:{port} не запущен.")
    except (TimeoutError, OSError) as error:
        print(f"Ошибка соединения: {error}")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Простой TCP-клиент чата")
    parser.add_argument("--host", default="127.0.0.1", help="Адрес сервера")
    parser.add_argument("--port", type=int, default=8888, help="TCP-порт сервера")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_arguments()
    run_client(arguments.host, arguments.port)
