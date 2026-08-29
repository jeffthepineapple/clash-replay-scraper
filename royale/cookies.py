"""Find a logged-in RoyaleAPI session in whatever browser this machine has.

Browser- and OS-agnostic: every browser browser_cookie3 knows about is tried,
whichever ones exist here answer, and the caller picks. All we want is the
session cookie -- the Cloudflare pass comes from our own browser instance.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import browser_cookie3

DOMAIN = "royaleapi.com"
SESSION_COOKIE = "__royaleapi_session_v2"

# Every extractor browser_cookie3 ships. Missing browsers just raise and are skipped.
BROWSERS = ("chrome", "chromium", "brave", "edge", "vivaldi", "opera", "opera_gx",
            "arc", "firefox", "librewolf", "safari")


@dataclass(frozen=True)
class Session:
    browser: str
    value: str

    def __str__(self) -> str:
        return f"{self.browser} ({self.value[:8]}...)"


def _prepare_env() -> None:
    """Chromium jars on Linux are keyring-encrypted and browser_cookie3 reaches
    the keyring over dbus; the bus address is not always exported to us. No-op
    anywhere the variable or the socket does not apply."""
    if os.name != "posix" or "DBUS_SESSION_BUS_ADDRESS" in os.environ:
        return
    try:
        sock = f"/run/user/{os.getuid()}/bus"
    except AttributeError:  # no getuid: not this kind of posix
        return
    if os.path.exists(sock):
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={sock}"


def find_sessions() -> list[Session]:
    """Every distinct RoyaleAPI session cookie found across local browsers."""
    _prepare_env()
    out: list[Session] = []
    seen: set[str] = set()
    for name in BROWSERS:
        fn = getattr(browser_cookie3, name, None)
        if fn is None:
            continue
        try:
            jar = fn(domain_name=DOMAIN)
        except Exception:
            continue  # browser absent, locked, or unsupported on this OS
        for c in jar:
            if c.name == SESSION_COOKIE and c.value not in seen:
                seen.add(c.value)
                out.append(Session(name, c.value))
    return out
