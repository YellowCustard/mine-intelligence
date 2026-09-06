"""Teltonika Codec 8 / 8E adapter — real GNSS trackers over raw TCP (brief §7, §10).

Trackers speak their own binary protocol, not MQTT, so this adapter decodes it and
republishes ``asset.position.v1`` into MQTT — indistinguishable at the ingest
boundary from the simulator (brief §10). The pure codec (``decode_packet`` and
friends) is separated from the socket I/O so it is exhaustively testable against
synthetic frames; the listener wires it to a TCP server and the MQTT publisher.

Wire format (Teltonika, big-endian):

    IMEI handshake:  <2-byte length><IMEI ASCII>  ->  server replies 0x01 accept / 0x00 reject
    AVL packet:      <4-byte preamble 0x00000000><4-byte data length><data><4-byte CRC-16>
                     ...server replies with a 4-byte count of records accepted.

The CRC is CRC-16/IBM over the data field. Malformed frames are rejected loudly
(brief §12): the connection is dropped without acknowledgement, so a device that
sends garbage is never told its data was stored.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

from minemonitor.ingest.adapters.base import DeviceAdapter
from minemonitor.ingest.service import PositionIngest

log = logging.getLogger("minemonitor.ingest.teltonika")

CODEC_8 = 0x08
CODEC_8E = 0x8E
IMEI_ACCEPT = b"\x01"
IMEI_REJECT = b"\x00"
# FMB ignition is reported on AVL IO id 239 (0/1); device-specific but standard.
IGNITION_IO_ID = 239


def crc16_ibm(data: bytes) -> int:
    """CRC-16/IBM (a.k.a. CRC-16/ARC): poly 0xA001, init 0. Teltonika's frame CRC."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def parse_imei(data: bytes) -> str | None:
    """Parse the IMEI handshake (``<2-byte length><ASCII digits>``), or None."""
    if len(data) < 2:
        return None
    length = int.from_bytes(data[0:2], "big")
    imei = data[2 : 2 + length]
    if len(imei) != length:
        return None
    text = imei.decode("ascii", "ignore")
    return text if text.isdigit() and text else None


@dataclass
class AvlRecord:
    """One decoded AVL record: a GPS fix plus its IO map."""

    ts: datetime
    priority: int
    lon: float
    lat: float
    altitude_m: int
    angle_deg: int
    satellites: int
    speed_kph: int
    event_io_id: int
    io: dict[int, int] = field(default_factory=dict)

    @property
    def ignition(self) -> bool | None:
        if IGNITION_IO_ID not in self.io:
            return None
        return bool(self.io[IGNITION_IO_ID])


@dataclass
class DecodedPacket:
    codec_id: int
    records: list[AvlRecord]


def _u(data: bytes, off: int, n: int) -> tuple[int, int]:
    return int.from_bytes(data[off : off + n], "big"), off + n


def _s(data: bytes, off: int, n: int) -> tuple[int, int]:
    return int.from_bytes(data[off : off + n], "big", signed=True), off + n


def _parse_io(data: bytes, off: int, extended: bool) -> tuple[int, dict[int, int], int]:
    """Parse an IO element (Codec 8 uses 1-byte counts/ids; 8E uses 2-byte)."""
    w = 2 if extended else 1  # width of id and count fields
    event_io_id, off = _u(data, off, w)
    _total, off = _u(data, off, w)  # total IO count, not needed for decoding
    io: dict[int, int] = {}
    for value_size in (1, 2, 4, 8):
        count, off = _u(data, off, w)
        for _ in range(count):
            io_id, off = _u(data, off, w)
            value, off = _u(data, off, value_size)
            io[io_id] = value
    if extended:
        # Codec 8E adds a variable-length (NX) section: <2-byte count>{id,len,bytes}.
        nx, off = _u(data, off, 2)
        for _ in range(nx):
            _io_id, off = _u(data, off, 2)
            length, off = _u(data, off, 2)
            off += length  # variable payloads are not used for a position fix
    return event_io_id, io, off


def _parse_record(data: bytes, off: int, extended: bool) -> tuple[AvlRecord, int]:
    ts_ms, off = _u(data, off, 8)
    priority, off = _u(data, off, 1)
    lon_raw, off = _s(data, off, 4)
    lat_raw, off = _s(data, off, 4)
    altitude, off = _s(data, off, 2)
    angle, off = _u(data, off, 2)
    satellites, off = _u(data, off, 1)
    speed, off = _u(data, off, 2)
    event_io_id, io, off = _parse_io(data, off, extended)
    record = AvlRecord(
        ts=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
        priority=priority,
        lon=lon_raw / 1e7,
        lat=lat_raw / 1e7,
        altitude_m=altitude,
        angle_deg=angle,
        satellites=satellites,
        speed_kph=speed,
        event_io_id=event_io_id,
        io=io,
    )
    return record, off


def decode_packet(frame: bytes) -> DecodedPacket:
    """Decode a full AVL frame (preamble..CRC). Raises ValueError if malformed."""
    if len(frame) < 12:
        raise ValueError("frame too short")
    if frame[0:4] != b"\x00\x00\x00\x00":
        raise ValueError("bad preamble")
    data_len = int.from_bytes(frame[4:8], "big")
    data = frame[8 : 8 + data_len]
    if len(data) != data_len:
        raise ValueError("truncated data field")
    crc_field = frame[8 + data_len : 8 + data_len + 4]
    if len(crc_field) != 4:
        raise ValueError("missing CRC")
    expected = int.from_bytes(crc_field, "big")
    if crc16_ibm(data) != expected:
        raise ValueError("CRC mismatch")

    codec_id = data[0]
    if codec_id not in (CODEC_8, CODEC_8E):
        raise ValueError(f"unsupported codec 0x{codec_id:02X}")
    extended = codec_id == CODEC_8E
    count = data[1]
    off = 2
    records: list[AvlRecord] = []
    for _ in range(count):
        record, off = _parse_record(data, off, extended)
        records.append(record)
    trailing_count = data[off]
    if trailing_count != count:
        raise ValueError("record count mismatch")
    return DecodedPacket(codec_id=codec_id, records=records)


def ack_bytes(n: int) -> bytes:
    """The server's reply after an AVL packet: the count of records accepted."""
    return n.to_bytes(4, "big")


def records_to_positions(
    records: list[AvlRecord], *, site_id: str, asset_id: str, source: str
) -> list[PositionIngest]:
    """Map decoded records onto the position contract (validated by Pydantic)."""
    out: list[PositionIngest] = []
    for r in records:
        out.append(
            PositionIngest(
                site_id=site_id,
                asset_id=asset_id,
                ts=r.ts,
                lat=r.lat,
                lon=r.lon,
                altitude_m=float(r.altitude_m),
                speed_kph=float(r.speed_kph),
                heading_deg=float(r.angle_deg),
                satellites=r.satellites,
                ignition=r.ignition,
                source=source,
            )
        )
    return out


class TeltonikaReplayAdapter(DeviceAdapter):
    """Replays captured Codec 8/8E frames as positions — the ``DeviceAdapter`` view.

    A live tracker's IMEI arrives only in the handshake, so a replay is told which
    asset the frames belong to. Useful for tests and for reprocessing a capture.
    """

    source = "teltonika:replay"

    def __init__(self, frames: list[bytes], *, site_id: str, asset_id: str) -> None:
        self._frames = frames
        self._site_id = site_id
        self._asset_id = asset_id

    def positions(self) -> Iterator[PositionIngest]:
        for frame in self._frames:
            packet = decode_packet(frame)
            yield from records_to_positions(
                packet.records, site_id=self._site_id, asset_id=self._asset_id, source=self.source
            )


def default_asset_for(imei: str) -> str:
    """Placeholder IMEI→asset mapping pending the real fleet list (brief §14)."""
    return f"teltonika-{imei}"


class TeltonikaServer:
    """Async TCP listener for live Teltonika trackers.

    Handshake → accept, then decode each AVL packet, publish its positions into
    MQTT, and acknowledge the record count. A malformed packet drops the
    connection unacknowledged so the device redelivers rather than losing data.
    """

    def __init__(
        self,
        publisher,
        *,
        host: str,
        port: int,
        site_id: str,
        source: str = "teltonika",
        imei_to_asset=default_asset_for,
    ) -> None:
        self._publisher = publisher
        self._host = host
        self._port = port
        self._site_id = site_id
        self._source = source
        self._imei_to_asset = imei_to_asset

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            length = int.from_bytes(await reader.readexactly(2), "big")
            imei = parse_imei((length).to_bytes(2, "big") + await reader.readexactly(length))
            if imei is None:
                writer.write(IMEI_REJECT)
                await writer.drain()
                return
            writer.write(IMEI_ACCEPT)
            await writer.drain()
            asset_id = self._imei_to_asset(imei)
            log.info("teltonika connected", extra={"site_id": self._site_id, "asset_id": asset_id})

            while True:
                head = await reader.readexactly(8)
                data_len = int.from_bytes(head[4:8], "big")
                frame = head + await reader.readexactly(data_len + 4)
                try:
                    packet = decode_packet(frame)
                except ValueError as exc:
                    log.error(
                        "rejected teltonika frame",
                        extra={"asset_id": asset_id, "error": str(exc)},
                    )
                    return  # drop unacknowledged
                positions = records_to_positions(
                    packet.records,
                    site_id=self._site_id,
                    asset_id=asset_id,
                    source=f"{self._source}:{imei}",
                )
                for payload in positions:
                    self._publisher.publish_position(payload)
                writer.write(ack_bytes(len(packet.records)))
                await writer.drain()
        except asyncio.IncompleteReadError:
            pass  # device disconnected
        finally:
            log.info("teltonika disconnected", extra={"peer": str(peer)})
            writer.close()

    async def serve(self) -> None:
        server = await asyncio.start_server(self._handle, self._host, self._port)
        log.info("teltonika listener up", extra={"host": self._host, "port": self._port})
        async with server:
            await server.serve_forever()


def main() -> None:
    """Run the live listener, republishing decoded positions into MQTT."""
    from minemonitor.config import get_settings
    from minemonitor.ingest.mqtt import MqttPublisher
    from minemonitor.logging_config import configure_logging

    settings = get_settings()
    configure_logging(settings.log_level)
    publisher = MqttPublisher()
    publisher.start()
    server = TeltonikaServer(
        publisher,
        host=settings.teltonika_host,
        port=settings.teltonika_port,
        site_id=settings.default_site_id,
    )
    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        pass
    finally:
        publisher.stop()


if __name__ == "__main__":
    main()
