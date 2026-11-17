"""
config.py - Reads credentials without ever hard-coding them.

Lookup order for each setting:
  1. Streamlit secrets (.streamlit/secrets.toml locally, or the "Secrets"
     box in Streamlit Community Cloud when deployed)
  2. Environment variables (including a local .env file, if python-dotenv
     is installed)

Required settings:
  SUPABASE_URL   - your project URL, e.g. https://abcd1234.supabase.co
  SUPABASE_KEY   - the Supabase secret / service key used in class
  APP_PASSWORD   - password for the classroom access gate
"""

import os

import streamlit as st

try:
    from dotenv import load_dotenv

    load_dotenv()  # loads a local .env file if one exists; harmless otherwise
except ImportError:
    pass


def get_setting(name: str) -> str | None:
    """Return a setting from Streamlit secrets, falling back to env variables."""
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        # Raised when no secrets.toml exists (e.g. local run using only .env).
        pass
    return os.getenv(name)
