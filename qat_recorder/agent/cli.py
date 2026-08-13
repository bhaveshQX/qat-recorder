# -*- coding: utf-8 -*-
"""
`qat-recorder-agent` — run the recorder agent on a VM.

    # once, per host
    qat-recorder-agent --generate-token > /etc/qatrec/token
    qat-recorder-agent --generate-cert --cert /etc/qatrec/agent.crt \\
                       --key /etc/qatrec/agent.key

    # then
    qat-recorder-agent --token-file /etc/qatrec/token \\
                       --cert /etc/qatrec/agent.crt --key /etc/qatrec/agent.key \\
                       --bind 0.0.0.0 --port 8765

On start it prints the certificate fingerprint. That is what testers pin, so
there is no CA to run and a swapped certificate cannot go unnoticed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_PORT = 8765


def _make_controller_factory():
    """Build real controllers, resolving qat lazily so --generate-token works
    on a machine that has not installed it yet."""
    def factory(app: str, lib: str, name: str):
        import qat
        from qat_recorder.ui.controller import RecorderController
        return RecorderController(
            qat, lib_path=lib, app_path=app, app_name=name or Path(app).name)
    return factory


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="qat-recorder-agent")
    parser.add_argument("--bind", default="127.0.0.1",
                        help="interface to listen on (default: loopback only)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token", help="shared token (prefer --token-file)")
    parser.add_argument("--token-file", help="file containing the shared token")
    parser.add_argument("--cert", help="TLS certificate")
    parser.add_argument("--key", help="TLS private key")
    parser.add_argument("--common-name", default="",
                        help="CN for --generate-cert (default: this hostname)")
    parser.add_argument("--generate-token", action="store_true",
                        help="print a new token and exit")
    parser.add_argument("--generate-cert", action="store_true",
                        help="write a self-signed certificate and exit")
    parser.add_argument("--insecure-plaintext", action="store_true",
                        help="serve without TLS (local testing only)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    from qat_recorder.agent import security

    if args.generate_token:
        print(security.generate_token())
        return 0

    if args.generate_cert:
        if not args.cert or not args.key:
            parser.error("--generate-cert needs --cert and --key")
        security.generate_self_signed(args.cert, args.key,
                                      args.common_name or None)
        print(f"certificate: {args.cert}")
        print(f"key        : {args.key}")
        print(f"fingerprint: {security.fingerprint(args.cert)}")
        return 0

    try:
        token = security.load_token(args.token, args.token_file)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    ssl_context = None
    if args.insecure_plaintext:
        # Refuse to be insecure by accident: plaintext on anything but loopback
        # would expose a process-launching service to the network.
        if args.bind not in ("127.0.0.1", "localhost", "::1"):
            print("--insecure-plaintext is only allowed on loopback",
                  file=sys.stderr)
            return 2
        print("WARNING: serving without TLS on loopback", file=sys.stderr)
    else:
        if not args.cert or not args.key:
            print("TLS requires --cert and --key (or --generate-cert first, or "
                  "--insecure-plaintext on loopback for a local test)",
                  file=sys.stderr)
            return 2
        ssl_context = security.server_context(args.cert, args.key)

    from qat_recorder.agent.server import Agent, AgentServer

    agent = Agent(token, controller_factory=_make_controller_factory())
    server = AgentServer((args.bind, args.port), agent,
                         ssl_context=ssl_context, verbose=args.verbose)

    print(f"qat-recorder-agent on {args.bind}:{server.port} "
          f"(host {agent.host_name})")
    if args.cert:
        print(f"pin this fingerprint: {security.fingerprint(args.cert)}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
