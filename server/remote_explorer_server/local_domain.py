from __future__ import annotations

import logging
import socket
import struct
import threading

LOGGER = logging.getLogger("remote_explorer.local_domain")

MDNS_ADDRESS = "224.0.0.251"
MDNS_PORT = 5353
MDNS_TTL_SECONDS = 120
DNS_TYPE_A = 1
DNS_TYPE_ANY = 255
DNS_CLASS_IN = 1


def local_domain_for_machine(*names: str | None) -> str:
    for name in names:
        label = sanitize_domain_label(name or "")
        if label:
            return f"{label}.local"
    return "remoteexplorer.local"


def sanitize_domain_label(value: str) -> str:
    chars: list[str] = []
    for char in value.strip().lower():
        if char.isspace():
            continue
        if "a" <= char <= "z" or "0" <= char <= "9":
            chars.append(char)
        elif char in {"-", "_", "."}:
            chars.append("-")
    label = "".join(chars).strip("-")
    while "--" in label:
        label = label.replace("--", "-")
    if len(label) > 63:
        label = label[:63].strip("-")
    return label


class MdnsHostResponder:
    def __init__(self, hostname: str, addresses: list[str]) -> None:
        self.hostname = _normalize_hostname(hostname)
        self.addresses = [address for address in addresses if _is_ipv4(address)]
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()

    def start(self) -> bool:
        if not self.hostname or not self.addresses:
            return False
        sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            sock.bind(("", MDNS_PORT))
            membership = socket.inet_aton(MDNS_ADDRESS) + socket.inet_aton("0.0.0.0")
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        except OSError as exc:
            LOGGER.warning("Could not register local mDNS host %s: %s", self.hostname, exc)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            return False

        self._socket = sock
        self._running.set()
        self._thread = threading.Thread(target=self._serve, name="RemoteExplorerMdns", daemon=True)
        self._thread.start()
        self.announce()
        LOGGER.info("Registered local mDNS host %s -> %s", self.hostname, ", ".join(self.addresses))
        return True

    def stop(self) -> None:
        self._running.clear()
        sock = self._socket
        self._socket = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def announce(self) -> None:
        sock = self._socket
        if sock is None:
            return
        try:
            sock.sendto(self._response_packet(), (MDNS_ADDRESS, MDNS_PORT))
        except OSError as exc:
            LOGGER.debug("Could not announce mDNS host %s: %s", self.hostname, exc)

    def _serve(self) -> None:
        sock = self._socket
        if sock is None:
            return
        while self._running.is_set():
            try:
                data, _address = sock.recvfrom(9000)
            except OSError:
                break
            if self._matches_query(data):
                try:
                    sock.sendto(self._response_packet(), (MDNS_ADDRESS, MDNS_PORT))
                except OSError as exc:
                    LOGGER.debug("Could not answer mDNS query for %s: %s", self.hostname, exc)

    def _matches_query(self, data: bytes) -> bool:
        try:
            questions = _parse_questions(data)
        except ValueError:
            return False
        expected = self.hostname.rstrip(".").lower()
        return any(name == expected and qtype in {DNS_TYPE_A, DNS_TYPE_ANY} for name, qtype, _qclass in questions)

    def _response_packet(self) -> bytes:
        header = struct.pack("!HHHHHH", 0, 0x8400, 0, len(self.addresses), 0, 0)
        answers = b"".join(_a_record(self.hostname, address) for address in self.addresses)
        return header + answers


def _a_record(hostname: str, address: str) -> bytes:
    return (
        _encode_name(hostname)
        + struct.pack("!HHIH", DNS_TYPE_A, 0x8000 | DNS_CLASS_IN, MDNS_TTL_SECONDS, 4)
        + socket.inet_aton(address)
    )


def _parse_questions(data: bytes) -> list[tuple[str, int, int]]:
    if len(data) < 12:
        raise ValueError("DNS packet is too short")
    _identifier, _flags, question_count, _answer_count, _authority_count, _additional_count = struct.unpack(
        "!HHHHHH",
        data[:12],
    )
    offset = 12
    questions: list[tuple[str, int, int]] = []
    for _ in range(question_count):
        name, offset = _decode_name(data, offset)
        if offset + 4 > len(data):
            raise ValueError("DNS question is truncated")
        qtype, qclass = struct.unpack("!HH", data[offset : offset + 4])
        offset += 4
        questions.append((name.rstrip(".").lower(), qtype, qclass))
    return questions


def _decode_name(data: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    jumped = False
    next_offset = offset
    seen: set[int] = set()
    while True:
        if offset >= len(data):
            raise ValueError("DNS name is truncated")
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("DNS compression pointer is truncated")
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if pointer in seen:
                raise ValueError("DNS compression pointer loop")
            seen.add(pointer)
            if not jumped:
                next_offset = offset + 2
            offset = pointer
            jumped = True
            continue
        if length == 0:
            if not jumped:
                next_offset = offset + 1
            break
        offset += 1
        if offset + length > len(data):
            raise ValueError("DNS label is truncated")
        labels.append(data[offset : offset + length].decode("ascii", errors="ignore"))
        offset += length
    return ".".join(labels), next_offset


def _encode_name(hostname: str) -> bytes:
    labels = hostname.rstrip(".").split(".")
    encoded = bytearray()
    for label in labels:
        raw = label.encode("ascii")
        if not raw or len(raw) > 63:
            raise ValueError(f"Invalid DNS label: {label!r}")
        encoded.append(len(raw))
        encoded.extend(raw)
    encoded.append(0)
    return bytes(encoded)


def _normalize_hostname(hostname: str) -> str:
    value = hostname.strip().lower().rstrip(".")
    if not value.endswith(".local"):
        value = f"{value}.local"
    return value


def _is_ipv4(address: str) -> bool:
    try:
        socket.inet_aton(address)
    except OSError:
        return False
    return address.count(".") == 3
