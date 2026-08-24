# -*- coding: utf-8 -*-
"""
Web interface for QAT Recorder.

A FastAPI server that wraps the existing Agent class and serves a browser-based
control panel. Replaces the PySide desktop panel and the stdlib HTTP server with
a modern web stack, optionally exposed through an ngrok tunnel so the tester's
browser can reach a VM without VPN, SSH tunnels or manual TLS setup.
"""
