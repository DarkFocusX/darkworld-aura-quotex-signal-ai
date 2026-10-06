#!/usr/bin/env python3
"""FIXTURE WebSocket stream — for plumbing verification only.

This is **not** a market-data provider and it is never used by the API, the scanner or
the Telegram gate. It exists so an operator (or a developer) can prove, end to end and
without credentials, that the live-feed path works:

    fixture socket  →  tools/start_live_feed.py  →  POST /api/data/ingest
                    →  M1 builder  →  rolling store  →  freshness  →  dashboard header

Every number it emits is generated (a seeded random walk), the banner says so, and the
rows it produces must be labelled as a fixture by whoever runs the bridge
(``LIVE_FEED_PROVIDER_LABEL=fixture_ws_selftest``). It refuses to run unless
``--i-know-this-is-a-fixture`` is passed, so it can never be mistaken for a real feed.

Usage (verification only):
    python3 tools/fixture_feed_server.py --i-know-this-is-a-fixture --port 8899 --asset USDJPY
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import random
import socket
import struct
import sys
import threading
import time
from datetime import datetime, timezone

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
BANNER = (
    "\n"
    "  ============================================================================\n"
    "   FIXTURE WEBSOCKET STREAM — GENERATED NUMBERS, NOT MARKET DATA.\n"
    "   Verification only. Never trade on this, never label it LIVE, never point\n"
    "   the production feed at it. Rows must carry a fixture provider label.\n"
    "  ============================================================================\n"
)


def handshake(conn: socket.socket) -> bool:
    data = b""
    conn.settimeout(5.0)
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            return False
        data += chunk
        if len(data) > 65536:
            return False
    headers = {}
    for line in data.decode("latin-1").split("\r\n")[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    key = headers.get("sec-websocket-key")
    if not key:
        return False
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    response = ("HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n")
    conn.sendall(response.encode("latin-1"))
    return True


def send_text(conn: socket.socket, payload: str) -> None:
    body = payload.encode("utf-8")
    header = bytearray([0x81])                       # FIN + text frame
    length = len(body)
    if length < 126:
        header.append(length)
    elif length < (1 << 16):
        header.append(126)
        header += struct.pack("!H", length)
    else:
        header.append(127)
        header += struct.pack("!Q", length)
    conn.sendall(bytes(header) + body)


def read_frames(conn: socket.socket, stop: threading.Event) -> None:
    """Drain (and ignore) client frames so the socket never stalls; log subscribes."""
    while not stop.is_set():
        try:
            head = conn.recv(2)
            if not head:
                stop.set()
                return
            opcode = head[0] & 0x0F
            masked = bool(head[1] & 0x80)
            length = head[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", conn.recv(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", conn.recv(8))[0]
            mask = conn.recv(4) if masked else None
            payload = b""
            while len(payload) < length:
                chunk = conn.recv(length - len(payload))
                if not chunk:
                    stop.set()
                    return
                payload += chunk
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x1:
                try:
                    parsed = json.loads(payload.decode("utf-8", "replace"))
                    print(f"[fixture] client message: {parsed}", flush=True)
                except Exception:                                    # noqa: BLE001
                    pass
            elif opcode == 0x8:                                      # close
                stop.set()
                return
        except (socket.timeout, OSError):
            if stop.is_set():
                return
            continue


def serve_client(conn: socket.socket, args: argparse.Namespace) -> None:
    if not handshake(conn):
        conn.close()
        return
    print(f"[fixture] client connected from {conn.getpeername()}", flush=True)
    stop = threading.Event()
    reader = threading.Thread(target=read_frames, args=(conn, stop), daemon=True)
    reader.start()

    rng = random.Random(args.seed)
    price = args.start_price
    conn.settimeout(1.0)
    interval = 1.0 / max(args.rate, 0.2)
    print(f"[fixture] streaming {args.asset} ticks at {args.rate}/s "
          f"(generated walk, start {price})", flush=True)
    sent = 0
    try:
        while not stop.is_set():
            cycle_start = time.time()
            now = datetime.now(tz=timezone.utc)
            for _ in range(args.ticks_per_batch):
                price = round(max(0.01, price + rng.gauss(0, args.volatility)), args.decimals)
                stamp = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
                send_text(conn, json.dumps({"type": "tick", "symbol": args.asset,
                                            "price": price, "ts": stamp}))
                sent += 1
                if args.heartbeats and sent % args.heartbeat_every == 0:
                    send_text(conn, json.dumps({"type": "heartbeat", "ts": stamp}))
            if args.inject_invalid and sent % args.invalid_every < args.ticks_per_batch:
                # labelled noise so the bridge's rejection counters are exercised too
                send_text(conn, json.dumps({"type": "candle", "symbol": args.asset,
                                            "open": 1.0, "close": 2.0,
                                            "ts": now.isoformat().replace("+00:00", "Z")}))
                send_text(conn, json.dumps({"type": "tick", "symbol": "FIXTURE-UNKNOWN",
                                            "price": 1.0,
                                            "ts": now.isoformat().replace("+00:00", "Z")}))
            if args.seconds and time.time() - args.started > args.seconds:
                print(f"[fixture] window elapsed after {sent} ticks", flush=True)
                break
            time.sleep(max(0.0, interval - (time.time() - cycle_start) / max(args.ticks_per_batch, 1)))
    except (BrokenPipeError, ConnectionResetError, OSError):
        print("[fixture] client disconnected", flush=True)
    finally:
        stop.set()
        try:
            conn.close()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--i-know-this-is-a-fixture", action="store_true",
                        help="required acknowledgement that this emits generated numbers")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--asset", default="USDJPY")
    parser.add_argument("--start-price", type=float, default=149.10)
    parser.add_argument("--rate", type=float, default=4.0, help="batches per second")
    parser.add_argument("--ticks-per-batch", type=int, default=1)
    parser.add_argument("--volatility", type=float, default=0.03)
    parser.add_argument("--decimals", type=int, default=3)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--seconds", type=float, default=0.0, help="serve at least this long")
    parser.add_argument("--heartbeats", action="store_true", default=True)
    parser.add_argument("--heartbeat-every", type=int, default=50)
    parser.add_argument("--inject-invalid", action="store_true",
                        help="also send one partial candle + one unknown instrument per cycle")
    parser.add_argument("--invalid-every", type=int, default=25)
    args = parser.parse_args()
    if not args.i_know_this_is_a_fixture:
        print(BANNER)
        print("Refusing to run: add --i-know-this-is-a-fixture to confirm you understand that "
              "this stream is generated and must never be presented as market data.")
        return 2
    print(BANNER, flush=True)
    args.started = time.time()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.host, args.port))
    server.listen(5)
    server.settimeout(1.0)
    print(f"[fixture] listening on ws://{args.host}:{args.port}  (Ctrl+C to stop)", flush=True)
    try:
        while True:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                if args.seconds and time.time() - args.started > args.seconds + 10:
                    break
                continue
            threading.Thread(target=serve_client, args=(conn, args), daemon=True).start()
    except KeyboardInterrupt:
        print("\n[fixture] stopped", flush=True)
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
