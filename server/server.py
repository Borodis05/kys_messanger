import argparse
import socket


BUFFER_SIZE = 4096


def handle_client(client_socket: socket.socket, client_address: tuple[str, int]) -> None:
    """Receive newline-delimited messages from one connected client."""
    print(f"[+] Клиент подключён: {client_address[0]}:{client_address[1]}")

    with client_socket:
        reader = client_socket.makefile("r", encoding="utf-8", newline="\n")

        try:
            for line in reader:
                message = line.rstrip("\r\n")
                if not message:
                    continue

                print(f"[{client_address[0]}:{client_address[1]}] {message}")

                response = f"Сервер получил: {message}\n"
                client_socket.sendall(response.encode("utf-8"))
        except (ConnectionError, OSError) as error:
            print(f"[!] Ошибка соединения с клиентом: {error}")
        finally:
            reader.close()
            print(f"[-] Клиент отключён: {client_address[0]}:{client_address[1]}")


def run_server(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((host, port))
        server_socket.listen(1)

        print(f"Сервер запущен на {host}:{port}")
        print("Ожидание клиента. Для остановки нажмите Ctrl+C.")

        try:
            while True:
                client_socket, client_address = server_socket.accept()
                handle_client(client_socket, client_address)
        except KeyboardInterrupt:
            print("\nСервер остановлен.")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Простой TCP-сервер чата")
    parser.add_argument("--host", default="127.0.0.1", help="Адрес для прослушивания")
    parser.add_argument("--port", type=int, default=8888, help="TCP-порт")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_arguments()
    run_server(arguments.host, arguments.port)
