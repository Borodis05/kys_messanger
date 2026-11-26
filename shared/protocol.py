import asyncio
import json
import uuid
from typing import Any, Dict, Optional


MAX_PACKET_SIZE = 16_384


class ProtocolError(Exception):
    def __init__(
        self, code: str, message: str, request_id: Optional[str] = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id


def new_request_id() -> str:
    return uuid.uuid4().hex


def encode_packet(packet: Dict[str, Any]) -> bytes:
    return (
        json.dumps(packet, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


async def write_packet(
    writer: asyncio.StreamWriter, packet: Dict[str, Any]
) -> None:
    data = encode_packet(packet)
    if len(data) > MAX_PACKET_SIZE:
        raise ProtocolError("packet_too_large", "JSON-пакет превышает допустимый размер.")

    writer.write(data)
    await writer.drain()


async def read_packet(
    reader: asyncio.StreamReader,
) -> Optional[Dict[str, Any]]:
    try:
        data = await reader.readline()
    except ValueError as error:
        raise ProtocolError(
            "packet_too_large", "JSON-пакет превышает допустимый размер."
        ) from error

    if not data:
        return None
    if len(data) > MAX_PACKET_SIZE:
        raise ProtocolError(
            "packet_too_large", "JSON-пакет превышает допустимый размер."
        )

    try:
        packet = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise ProtocolError("invalid_encoding", "Пакет должен быть в UTF-8.") from error
    except json.JSONDecodeError as error:
        raise ProtocolError("invalid_json", "Получен некорректный JSON.") from error

    if not isinstance(packet, dict):
        raise ProtocolError("invalid_packet", "JSON-пакет должен быть объектом.")

    return packet
