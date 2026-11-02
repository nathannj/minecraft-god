import struct

from god_bridge import rcon

from god_bridge.rcon import SERVERDATA_EXECCOMMAND, decode_packet, encode_packet


def test_encode_packet_uses_little_endian_rcon_layout():
    packet = encode_packet(42, SERVERDATA_EXECCOMMAND, "time set noon")
    (length,) = struct.unpack("<i", packet[:4])
    assert length == len(packet) - 4
    assert decode_packet(packet[4:]) .body == "time set noon"


def test_decode_packet_rejects_short_payload():
    try:
        decode_packet(b"too-short")
    except ValueError as exc:
        assert "too short" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_command_assembles_paper_multi_packet_responses(monkeypatch):
    class FakeSocket:
        def __init__(self):
            self.pending = bytearray()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def settimeout(self, timeout):
            self.timeout = timeout

        def sendall(self, packet):
            request_id = struct.unpack("<i", packet[4:8])[0]
            packet_type = struct.unpack("<i", packet[8:12])[0]
            if packet_type == rcon.SERVERDATA_AUTH:
                self.pending.extend(rcon.encode_packet(request_id, rcon.SERVERDATA_AUTH, ""))
            else:
                body = packet[12:-2].decode("utf-8")
                if body:
                    output = "a" * 4096 + '{"complete":true}'
                    self.pending.extend(rcon.encode_packet(request_id, rcon.SERVERDATA_RESPONSE_VALUE, output[:4096]))
                    self.pending.extend(rcon.encode_packet(request_id, rcon.SERVERDATA_RESPONSE_VALUE, output[4096:]))
                else:
                    self.pending.extend(rcon.encode_packet(request_id, rcon.SERVERDATA_RESPONSE_VALUE, ""))

        def recv(self, size):
            if not self.pending:
                return b""
            chunk = bytes(self.pending[:size])
            del self.pending[:size]
            return chunk

    fake_socket = FakeSocket()
    monkeypatch.setattr(rcon.socket, "create_connection", lambda address, timeout: fake_socket)

    response = rcon.RconClient("127.0.0.1", 25575, "password").command("godscan Player 8 5")

    assert len(response.body) > 4096
    assert response.body.endswith('{"complete":true}')


def test_command_keeps_response_when_paper_omits_empty_sentinel(monkeypatch):
    class FakeSocket:
        def __init__(self):
            self.pending = bytearray()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def settimeout(self, timeout):
            self.timeout = timeout

        def sendall(self, packet):
            request_id = struct.unpack("<i", packet[4:8])[0]
            packet_type = struct.unpack("<i", packet[8:12])[0]
            if packet_type == rcon.SERVERDATA_AUTH:
                self.pending.extend(rcon.encode_packet(request_id, rcon.SERVERDATA_AUTH, ""))
            elif packet[12:-2].decode("utf-8") != "list":
                self.pending.extend(rcon.encode_packet(request_id, rcon.SERVERDATA_RESPONSE_VALUE, "There are 0 players online"))

        def recv(self, size):
            if not self.pending:
                return b""
            chunk = bytes(self.pending[:size])
            del self.pending[:size]
            return chunk

    fake_socket = FakeSocket()
    monkeypatch.setattr(rcon.socket, "create_connection", lambda address, timeout: fake_socket)

    response = rcon.RconClient("127.0.0.1", 25575, "password").command("time query daytime")

    assert response.body == "There are 0 players online"


def test_marker_waits_until_real_command_has_responded(monkeypatch):
    class OrderedSocket:
        def __init__(self):
            self.buffer = bytearray()
            self.command_response_read = False
        def sendall(self, packet):
            value = decode_packet(packet[4:])
            if value.body == "list":
                assert self.command_response_read, "Paper can reject coalesced command requests"
            self.buffer.extend(encode_packet(value.request_id, 0, "marker" if value.body == "list" else "confirmed"))
        def recv(self, size):
            result = bytes(self.buffer[:size])
            del self.buffer[:size]
            if not self.buffer:
                self.command_response_read = True
            return result
    response = rcon.RconClient("localhost", 25575, "")._command(OrderedSocket(), "godcheckbatch minecraft:overworld 0,64,0")
    assert response.body == "confirmed"
