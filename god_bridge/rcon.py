"""Small dependency-free Minecraft RCON client."""

from __future__ import annotations

import random
import socket
import struct
from dataclasses import dataclass


SERVERDATA_RESPONSE_VALUE = 0
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_AUTH = 3


@dataclass(frozen=True)
class RconResponse:
    request_id: int
    packet_type: int
    body: str


def encode_packet(request_id: int, packet_type: int, body: str) -> bytes:
    payload = struct.pack("<ii", request_id, packet_type)
    payload += body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


def decode_packet(packet: bytes) -> RconResponse:
    if len(packet) < 10:
        raise ValueError("RCON packet is too short")
    request_id, packet_type = struct.unpack("<ii", packet[:8])
    body = packet[8:-2].decode("utf-8", errors="replace")
    if not packet.endswith(b"\x00\x00"):
        raise ValueError("RCON packet is missing its terminators")
    return RconResponse(request_id, packet_type, body)


class RconClient:
    def __init__(self, host: str, port: int, password: str, timeout: float = 4.0) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout

    def _read_packet(self, sock: socket.socket) -> RconResponse:
        header = _read_exact(sock, 4)
        (length,) = struct.unpack("<i", header)
        if length < 10 or length > 10_000_000:
            raise ValueError(f"Invalid RCON packet length: {length}")
        return decode_packet(_read_exact(sock, length))

    def _request(self, sock: socket.socket, packet_type: int, body: str) -> RconResponse:
        request_id = random.randint(1, 2_000_000_000)
        sock.sendall(encode_packet(request_id, packet_type, body))
        response = self._read_packet(sock)
        if response.request_id == -1:
            raise PermissionError("Minecraft RCON authentication failed")
        return response

    def _command(self, sock: socket.socket, command: str) -> RconResponse:
        """Run a command and assemble Paper's multi-packet response.

        Paper sends command output in chunks of up to 4096 characters.  A
        second, harmless `list` command acts as an in-order sentinel, allowing
        us to know when every chunk for the real command has arrived. Paper
        may close an RCON connection when sent an empty command, especially
        after plugin-defined commands, so an empty sentinel is not reliable.
        """
        request_id = random.randint(1, 2_000_000_000)
        sentinel_id = random.randint(1, 2_000_000_000)
        while sentinel_id == request_id:
            sentinel_id = random.randint(1, 2_000_000_000)
        sock.sendall(encode_packet(request_id, SERVERDATA_EXECCOMMAND, command))
        sock.sendall(encode_packet(sentinel_id, SERVERDATA_EXECCOMMAND, "list"))

        chunks: list[str] = []
        while True:
            try:
                response = self._read_packet(sock)
            except (ConnectionError, TimeoutError, socket.timeout):
                # Some Paper builds return every packet for the real command
                # and then close or stop responding instead of acknowledging
                # the empty sentinel. The command result is still complete and
                # must not be discarded merely because that optional marker is
                # absent. With no result packets, preserve the original error.
                if chunks:
                    break
                raise
            if response.request_id == sentinel_id:
                break
            if response.request_id == request_id:
                chunks.append(response.body)
        return RconResponse(request_id, SERVERDATA_RESPONSE_VALUE, "".join(chunks))

    def command(self, command: str) -> RconResponse:
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.settimeout(self.timeout)
            auth = self._request(sock, SERVERDATA_AUTH, self.password)
            if auth.request_id == -1:
                raise PermissionError("Minecraft RCON authentication failed")
            return self._command(sock, command)


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("Minecraft RCON connection closed unexpectedly")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
