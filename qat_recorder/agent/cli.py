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
    # First line of every session's log. Four wheels with the same file name
    # have crossed to the same VM; this is what says which one arrived.
    from qat_recorder import provenance

    print(provenance.one_line())

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
    parser.add_argument("--ngrok", action="store_true",
                        help="expose the agent via an ngrok tunnel")
    parser.add_argument("--ngrok-authtoken", default="",
                        help="ngrok auth token (or set NGROK_AUTHTOKEN env var)")
    parser.add_argument("--ngrok-domain", default="",
                        help="custom ngrok domain to use")
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
    if args.insecure_plaintext or args.ngrok:
        if args.insecure_plaintext and args.bind not in ("127.0.0.1", "localhost", "::1") and not args.ngrok:
            print("--insecure-plaintext is only allowed on loopback",
                  file=sys.stderr)
            return 2
        if args.insecure_plaintext:
            print("WARNING: serving without TLS on loopback", file=sys.stderr)
    else:
        if not args.cert or not args.key:
            print("TLS requires --cert and --key (or --generate-cert first, or "
                  "--ngrok, or --insecure-plaintext on loopback for a local test)",
                  file=sys.stderr)
            return 2

    from qat_recorder.agent.server import Agent
    from qat_recorder.web.app import run_server

    agent = Agent(token, controller_factory=_make_controller_factory())
    
    if args.cert and not args.ngrok:
        print(f"pin this fingerprint: {security.fingerprint(args.cert)}")

    run_server(
        agent,
        token=token,
        host=args.bind,
        port=args.port,
        ngrok=args.ngrok,
        ngrok_authtoken=args.ngrok_authtoken,
        ngrok_domain=args.ngrok_domain,
        verbose=args.verbose,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
