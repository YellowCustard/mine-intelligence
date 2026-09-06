"""Teltonika Codec 8 / 8E decoding and the TCP listener (M7)."""

from __future__ import annotations

import asyncio

import pytest

from minemonitor.ingest.adapters.teltonika import (
    IMEI_ACCEPT,
    IMEI_REJECT,
    TeltonikaReplayAdapter,
    TeltonikaServer,
    ack_bytes,
    crc16_ibm,
    decode_packet,
    parse_imei,
    records_to_positions,
)

# A Zimbabwe fix: lat -17.8252, lon 31.0335, 47 km/h, heading 118, 11 sats, 1483 m.
_LAT_RAW = -178_252_000
_LON_RAW = 310_335_000
_TS_MS = 1_757_000_000_000


def _gps() -> bytes:
    b = (_TS_MS).to_bytes(8, "big")
    b += (1).to_bytes(1, "big")  # priority
    b += (_LON_RAW).to_bytes(4, "big", signed=True)
    b += (_LAT_RAW).to_bytes(4, "big", signed=True)
    b += (1483).to_bytes(2, "big", signed=True)  # altitude
    b += (118).to_bytes(2, "big")  # angle
    b += (11).to_bytes(1, "big")  # satellites
    b += (47).to_bytes(2, "big")  # speed
    return b


def _record_c8(io1: dict[int, int]) -> bytes:
    b = _gps()
    b += (0).to_bytes(1, "big")  # event io id
    b += len(io1).to_bytes(1, "big")  # total io
    b += len(io1).to_bytes(1, "big")  # N1 (1-byte values)
    for iid, val in io1.items():
        b += iid.to_bytes(1, "big") + val.to_bytes(1, "big")
    b += b"\x00\x00\x00"  # N2, N4, N8 all zero
    return b


def _record_c8e(io1: dict[int, int]) -> bytes:
    b = _gps()
    b += (0).to_bytes(2, "big")  # event io id (2-byte in 8E)
    b += len(io1).to_bytes(2, "big")  # total io
    b += len(io1).to_bytes(2, "big")  # N1
    for iid, val in io1.items():
        b += iid.to_bytes(2, "big") + val.to_bytes(1, "big")
    b += (0).to_bytes(2, "big") * 3  # N2, N4, N8
    b += (0).to_bytes(2, "big")  # NX variable count
    return b


def _frame(codec_id: int, records: list[bytes]) -> bytes:
    data = codec_id.to_bytes(1, "big") + len(records).to_bytes(1, "big")
    data += b"".join(records) + len(records).to_bytes(1, "big")
    return (
        b"\x00\x00\x00\x00"
        + len(data).to_bytes(4, "big")
        + data
        + crc16_ibm(data).to_bytes(4, "big")
    )


def test_crc16_known_vector() -> None:
    # The standard CRC-16/ARC (IBM) check value for "123456789".
    assert crc16_ibm(b"123456789") == 0xBB3D


def test_parse_imei() -> None:
    imei = "356307042441013"
    assert parse_imei(len(imei).to_bytes(2, "big") + imei.encode()) == imei
    assert parse_imei(b"\x00\x02" + b"ab") is None  # non-digit
    assert parse_imei(b"\x00\x05" + b"123") is None  # truncated


def test_decode_codec8_fix() -> None:
    packet = decode_packet(_frame(0x08, [_record_c8({239: 1})]))
    assert packet.codec_id == 0x08
    (r,) = packet.records
    assert round(r.lat, 4) == -17.8252
    assert round(r.lon, 4) == 31.0335
    assert r.speed_kph == 47 and r.angle_deg == 118 and r.satellites == 11
    assert r.ignition is True


def test_decode_codec8_extended() -> None:
    packet = decode_packet(_frame(0x8E, [_record_c8e({239: 0})]))
    assert packet.codec_id == 0x8E
    (r,) = packet.records
    assert round(r.lat, 4) == -17.8252 and round(r.lon, 4) == 31.0335
    assert r.ignition is False


def test_decode_multiple_records() -> None:
    packet = decode_packet(_frame(0x08, [_record_c8({}), _record_c8({239: 1})]))
    assert len(packet.records) == 2
    assert packet.records[0].ignition is None  # no IO 239


@pytest.mark.parametrize(
    "mangle",
    [
        lambda f: b"\x01" + f[1:],  # bad preamble
        lambda f: f[:-1] + bytes([f[-1] ^ 0xFF]),  # corrupt CRC
        lambda f: f[:9] + bytes([0x07]) + f[10:],  # unsupported codec id
    ],
)
def test_malformed_frames_rejected(mangle) -> None:
    frame = _frame(0x08, [_record_c8({})])
    with pytest.raises(ValueError):
        decode_packet(mangle(frame))


def test_record_count_mismatch_rejected() -> None:
    rec = _record_c8({})
    # Trailing count (2) disagrees with the leading count (1).
    data = b"\x08\x01" + rec + b"\x02"
    frame = (
        b"\x00\x00\x00\x00"
        + len(data).to_bytes(4, "big")
        + data
        + crc16_ibm(data).to_bytes(4, "big")
    )
    with pytest.raises(ValueError, match="record count mismatch"):
        decode_packet(frame)


def test_records_to_positions_maps_contract() -> None:
    packet = decode_packet(_frame(0x08, [_record_c8({239: 1})]))
    positions = records_to_positions(
        packet.records, site_id="kn-zw-01", asset_id="HT-102", source="teltonika:x"
    )
    (p,) = positions
    assert p.site_id == "kn-zw-01" and p.asset_id == "HT-102"
    assert round(p.lat, 4) == -17.8252 and p.speed_kph == 47.0
    assert p.ignition is True and p.source == "teltonika:x"


def test_replay_adapter_yields_positions() -> None:
    frames = [_frame(0x08, [_record_c8({})]), _frame(0x8E, [_record_c8e({})])]
    adapter = TeltonikaReplayAdapter(frames, site_id="kn-zw-01", asset_id="HT-102")
    positions = list(adapter.positions())
    assert len(positions) == 2
    assert all(p.asset_id == "HT-102" for p in positions)
    assert all(p.source == "teltonika:replay" for p in positions)


# --- Listener, driven through an in-memory stream (no sockets, deterministic) ---


class _FakePublisher:
    def __init__(self) -> None:
        self.published: list = []

    def publish_position(self, payload) -> None:
        self.published.append(payload)


class _FakeWriter:
    def __init__(self) -> None:
        self.buf = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buf += data

    async def drain(self) -> None:
        pass

    def get_extra_info(self, _key: str):
        return ("test", 0)

    def close(self) -> None:
        self.closed = True


def _drive(server: TeltonikaServer, incoming: bytes) -> _FakeWriter:
    async def run() -> _FakeWriter:
        reader = asyncio.StreamReader()
        reader.feed_data(incoming)
        reader.feed_eof()
        writer = _FakeWriter()
        await server._handle(reader, writer)
        return writer

    return asyncio.run(run())


def _handshake(imei: str) -> bytes:
    return len(imei).to_bytes(2, "big") + imei.encode()


def test_listener_accepts_and_publishes() -> None:
    pub = _FakePublisher()
    server = TeltonikaServer(pub, host="127.0.0.1", port=0, site_id="kn-zw-01")
    imei = "356307042441013"
    writer = _drive(server, _handshake(imei) + _frame(0x08, [_record_c8({239: 1})]))
    assert writer.buf[0:1] == IMEI_ACCEPT
    assert writer.buf[1:5] == ack_bytes(1)  # one record accepted
    assert writer.closed
    assert len(pub.published) == 1
    assert pub.published[0].asset_id == f"teltonika-{imei}"


def test_listener_rejects_bad_imei() -> None:
    pub = _FakePublisher()
    server = TeltonikaServer(pub, host="127.0.0.1", port=0, site_id="kn-zw-01")
    writer = _drive(server, b"\x00\x03" + b"abc")  # non-numeric IMEI
    assert writer.buf == IMEI_REJECT
    assert pub.published == []


def test_listener_drops_malformed_frame_unacked() -> None:
    pub = _FakePublisher()
    server = TeltonikaServer(pub, host="127.0.0.1", port=0, site_id="kn-zw-01")
    good = _frame(0x08, [_record_c8({})])
    bad = good[:-1] + bytes([good[-1] ^ 0xFF])  # corrupt CRC
    writer = _drive(server, _handshake("356307042441013") + bad)
    assert writer.buf == IMEI_ACCEPT  # accepted the handshake, but no ack for the bad frame
    assert pub.published == []
