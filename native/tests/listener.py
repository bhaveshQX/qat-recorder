# -*- coding: utf-8 -*-
"""
Minimal receiver for the qatrec event stream.

Listens on a port, accepts one connection from the injected library, writes each
newline-delimited JSON record to a file, and exits when the peer closes or the
timeout expires. Stands in for the Python capture layer during native testing.

    python3 listener.py <port> <output-file> [timeout-seconds]
"""

import json
import socket
import sys
import time


def main():
    port = int(sys.argv[1])
    out_path = sys.argv[2]
    timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 30.0

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(1)
    server.settimeout(timeout)
    print(f"listener: waiting on {port}", flush=True)

    try:
        conn, _ = server.accept()
    except socket.timeout:
        print("listener: no connection (filter never activated)", flush=True)
        open(out_path, "w").close()
        return 0

    print("listener: connected", flush=True)
    conn.settimeout(timeout)

    buffer = b""
    records = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            chunk = conn.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line.decode("utf-8")))
            except ValueError as error:
                print(f"listener: BAD JSON: {error}: {line[:200]!r}", flush=True)

    with open(out_path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")

    print(f"listener: {len(records)} record(s)", flush=True)
    kinds = {}
    for record in records:
        kinds[record.get("kind")] = kinds.get(record.get("kind"), 0) + 1
    for kind, count in sorted(kinds.items()):
        print(f"  {kind}: {count}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
