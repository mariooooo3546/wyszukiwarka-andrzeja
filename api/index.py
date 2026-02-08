"""Vercel serverless entry point."""
import sys
import os

# Dodaj katalog nadrzędny do path, aby importy z app.py działały
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402
