#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Xentinel AI (SaaS-ready)
Entry point for WSGI servers (Gunicorn) and local dev.
"""

from nexus import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
