#!/usr/bin/env python3
"""claude-usage-monitor: a tiny desktop widget for your Claude Pro/Max rate limits.

It reads the OAuth token that Claude Code already stores on your machine
(`~/.claude/.credentials.json`), asks Anthropic's usage endpoint how much of
your 5-hour / 7-day windows you've burned, and shows live bars + a weekly
heatmap. No accounts, no servers, no telemetry. Everything stays local.

Commands:
    claude-usage            Launch the desktop widget (default).
    claude-usage login      Detect your Claude account and print live usage.
    claude-usage status     Print current usage once and exit (no window).

See README.md for setup. MIT licensed.
"""

import argparse
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import zlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
ICON_PATH = APP_DIR / "claude_usage.png"

# Where Claude Code stores your OAuth token after you log in once.
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"

# Our own config/state dir (kept separate from Claude Code's; honors XDG).
_XDG_CONFIG = os.environ.get("XDG_CONFIG_HOME")
CONFIG_DIR = (Path(_XDG_CONFIG) if _XDG_CONFIG else Path.home() / ".config") / "claude-usage-monitor"
HISTORY_PATH = CONFIG_DIR / "history.json"
STATE_PATH = CONFIG_DIR / "state.json"
USER_CONFIG_PATH = CONFIG_DIR / "config.json"

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"

# Polling cadence (seconds).
DEFAULT_INTERVAL = 60
MIN_INTERVAL = 30
MAX_INTERVAL = 900

# Rate-limit (HTTP 429) backoff.
RATE_LIMIT_BASE_BACKOFF = 30
RATE_LIMIT_MAX_BACKOFF = 300

# Tor auto-fallback: Anthropic occasionally IP-rate-limits the usage endpoint;
# routing the request over Tor gets a fresh exit IP. Optional, only used if
# `tor` + `torsocks` + `curl` are installed and the Tor SOCKS port is open.
TOR_FALLBACK_429_THRESHOLD = 3   # switch to Tor after this many consecutive 429s
TOR_GIVE_UP_429_THRESHOLD = 5    # give up on Tor after this many consecutive 429s
TOR_SOCKS_HOST = "127.0.0.1"
TOR_SOCKS_PORT = 9050

WINDOW_5H_SECONDS = 5 * 3600
MAX_SNAPSHOT_AGE = 14 * 86400    # ignore cached state older than this
STALE_AFTER_SECONDS = 180        # mark the reading "stale" past this age

# Sepia theme.
BG = "#1a1210"
BG_LIGHT = "#2e2118"
BAR_BG = "#3d2e22"
TEXT = "#d4b896"
TEXT_BOLD = "#e8d5b5"
DIM = "#8a7560"
ACCENT = "#c89b6e"
GREEN = "#8fac5f"
YELLOW = "#d4a94c"
RED = "#c45c4a"
SEP = "#4a3828"
BTN_BG = "#3d2e22"
BTN_HOVER = "#4f3d2e"
FONT = "sans-serif"


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def _as_int(value, default=0):
    """Best-effort int coercion (credential timestamps are sometimes strings)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def read_credentials():
    """Read the Claude Code OAuth token from disk.

    Returns a dict with access_token / refresh_token / expires_at / plan info,
    or {"error": "..."} if the file is missing or malformed.
    """
    try:
        with open(CREDENTIALS_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"error": "not-logged-in"}
    except (json.JSONDecodeError, OSError) as e:
        return {"error": f"bad credentials file: {e}"}

    oauth = data.get("claudeAiOauth", {})
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        return {"error": "not-logged-in"}
    return {
        "access_token": oauth.get("accessToken", ""),
        "refresh_token": oauth.get("refreshToken", ""),
        "expires_at": _as_int(oauth.get("expiresAt", 0)),
        "subscription_type": oauth.get("subscriptionType", ""),
        "rate_limit_tier": oauth.get("rateLimitTier", ""),
    }


def is_token_expired(creds):
    return time.time() * 1000 >= _as_int(creds.get("expires_at", 0))


def _load_plan_override():
    """Optional manual plan override from config.json: {"plan": "max_20x"}."""
    try:
        with open(USER_CONFIG_PATH) as f:
            return (json.load(f).get("plan") or "").strip().lower() or None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def plan_label(creds):
    """Human label like 'Max 20x' / 'Max 5x' / 'Pro', from creds or override."""
    override = _load_plan_override()
    if override:
        return override.replace("_", " ").title()
    sub = (creds.get("subscription_type") or "Claude").replace("_", " ").title()
    tier = (creds.get("rate_limit_tier") or "").lower()
    if "20x" in tier:
        sub += " 20x"
    elif "5x" in tier:
        sub += " 5x"
    return sub


def is_max_plan(creds):
    override = _load_plan_override()
    blob = override or (creds.get("subscription_type", "") + creds.get("rate_limit_tier", ""))
    blob = blob.lower()
    return "max" in blob or "20x" in blob or "5x" in blob


# ---------------------------------------------------------------------------
# Usage API (with optional Tor fallback)
# ---------------------------------------------------------------------------

def _parse_iso_ts(s):
    """ISO 8601 string -> unix epoch seconds, or None."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _parse_retry_after(value):
    """Retry-After header (seconds or HTTP-date) -> seconds, or None."""
    if not value:
        return None
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        pass
    try:
        return max(0, int(parsedate_to_datetime(str(value)).timestamp() - time.time()))
    except (TypeError, ValueError, OverflowError):
        return None


def _normalize_util(val):
    """Normalize a utilization value to a 0..1 fraction.

    Anthropic's payloads are inconsistent around 1.0: values below 1 are
    already fractional (0.30 = 30%) while values >= 1.0 are percentages
    (1.0 means 1%, not 100%). Treat all values >= 1.0 as percentages.
    """
    if val is None:
        return None
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    if v >= 1.0:
        v /= 100.0
    return max(0.0, min(1.0, v))


def _parse_usage(data):
    """Turn the usage JSON into a flat dict of fractions + reset timestamps."""
    if not isinstance(data, dict):
        return {"error": "invalid usage payload"}

    def section(key):
        s = data.get(key) or {}
        return s if isinstance(s, dict) else {}

    fh, sd, sonnet = section("five_hour"), section("seven_day"), section("seven_day_sonnet")

    util_5h = _normalize_util(fh.get("utilization"))
    util_7d = _normalize_util(sd.get("utilization"))
    util_sonnet = _normalize_util(sonnet.get("utilization"))

    if util_5h is None and util_7d is None and util_sonnet is None:
        return {"error": "usage payload missing utilization fields"}

    limited = ((util_5h is not None and util_5h >= 1.0) or
               (util_7d is not None and util_7d >= 1.0) or
               (util_sonnet is not None and util_sonnet >= 1.0))

    return {
        "5h_util": util_5h,
        "7d_util": util_7d,
        "sonnet_util": util_sonnet,
        "5h_reset": _parse_iso_ts(fh.get("resets_at")),
        "7d_reset": _parse_iso_ts(sd.get("resets_at")),
        "sonnet_reset": _parse_iso_ts(sonnet.get("resets_at")),
        "status": "rejected" if limited else "allowed",
    }


def tor_available():
    """True if the bits needed for the Tor fallback are present and listening."""
    if not (shutil.which("torsocks") and shutil.which("curl")):
        return False
    return _tor_socks_up()


def _tor_socks_up(host=TOR_SOCKS_HOST, port=TOR_SOCKS_PORT, timeout=1.0):
    """Preflight: is the Tor SOCKS port accepting connections right now?"""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _is_transport_failure(result):
    return isinstance(result, dict) and result.get("transport_error") is True


def fetch_usage(access_token, use_tor=False):
    """Fetch usage, preferring the requested egress and falling back on the other.

    A *transport* failure (Tor down, timeout, connection reset) transparently
    retries the other path; a real HTTP answer (429/4xx) is returned as-is so
    the caller's backoff logic can see it. The returned dict carries `via` =
    the transport that actually produced it.
    """
    if not access_token:
        return {"error": "missing access token"}

    order = ("tor", "direct") if use_tor else ("direct", "tor")
    last = None
    for transport in order:
        if transport == "tor":
            if not tor_available():
                last = {"error": "Tor unavailable", "transport_error": True, "via": "tor"}
                continue
            result = _fetch_usage_tor(access_token)
        else:
            result = _fetch_usage_direct(access_token)
        result.setdefault("via", transport)
        if not _is_transport_failure(result):
            return result
        last = result
    return last if last is not None else {"error": "no transport available"}


class _StripAuthOnCrossHostRedirect(HTTPRedirectHandler):
    """Don't let the Authorization / beta headers follow a cross-host redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            try:
                same_host = urlparse(newurl).hostname == urlparse(req.full_url).hostname
            except Exception:
                same_host = False  # fail closed: when in doubt, strip the secrets
            if not same_host:
                new.headers = {k: v for k, v in new.headers.items()
                               if k.lower() not in ("authorization", "anthropic-beta")}
        return new


_OPENER = build_opener(_StripAuthOnCrossHostRedirect)


def _fetch_usage_direct(access_token):
    req = Request(USAGE_URL, method="GET")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("anthropic-beta", OAUTH_BETA)
    try:
        with _OPENER.open(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except HTTPError as e:
        out = {"error": f"HTTP {e.code}: {e.reason}", "http_code": e.code}
        retry = _parse_retry_after(e.headers.get("Retry-After") if e.headers else None)
        if retry is not None:
            out["retry_after"] = retry
        return out
    except URLError as e:
        return {"error": f"Connection error: {e.reason}", "transport_error": True}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"error": "invalid API JSON response"}
    except Exception as e:
        return {"error": str(e), "transport_error": True}
    return _parse_usage(data)


def _fetch_usage_tor(access_token):
    """Fetch usage via `torsocks curl` to dodge IP-based rate limits.

    The OAuth token is handed to curl through a stdin config file
    (`--config -`) rather than an `-H` argv argument, so it never appears in
    the process list, because `/proc/<pid>/cmdline` is world-readable on default Linux.
    """
    if not re.fullmatch(r"[\w.\-]+", access_token or "", re.ASCII):
        # Unexpected characters could break out of the curl config; use direct.
        return {"error": "token has unexpected characters; skipping Tor",
                "transport_error": True}
    cmd = [
        "torsocks", "curl", "-sS", "--connect-timeout", "10",
        "-H", f"anthropic-beta: {OAUTH_BETA}",
        "-w", "\n__HTTP_CODE__:%{http_code}",
        "--config", "-",
        USAGE_URL,
    ]
    stdin_config = f'header = "Authorization: Bearer {access_token}"\n'
    try:
        result = subprocess.run(cmd, input=stdin_config.encode(),
                                capture_output=True, timeout=20)
        if result.returncode != 0:
            err = result.stderr.decode() if result.stderr else "torsocks failed"
            return {"error": f"Tor error: {err[:80]}", "transport_error": True}
        raw = result.stdout
        http_code = None
        marker = b"\n__HTTP_CODE__:"
        if marker in raw:
            raw, code_raw = raw.rsplit(marker, 1)
            try:
                http_code = int(code_raw.strip()[:3])
            except ValueError:
                http_code = None
        if http_code == 429:
            return {"error": "HTTP 429: Rate limited (via Tor)", "http_code": 429, "retry_after": 0}
        if http_code is not None and http_code >= 400:
            return {"error": f"HTTP {http_code}: Tor request failed", "http_code": http_code}
        data = json.loads(raw.decode())
    except subprocess.TimeoutExpired:
        return {"error": "Tor timeout", "transport_error": True}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"error": "invalid API JSON via Tor", "transport_error": True}
    except Exception as e:
        return {"error": f"Tor exception: {e}", "transport_error": True}
    return _parse_usage(data)


# ---------------------------------------------------------------------------
# History & state (local, for the weekly heatmap and instant restore)
# ---------------------------------------------------------------------------

def _current_block_index():
    """5h block index for the current local hour (0-4)."""
    return min(time.localtime().tm_hour // 5, 4)


def load_history():
    try:
        with open(HISTORY_PATH) as f:
            data = json.load(f)
        return _sanitize_history(data) if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _sanitize_history(history):
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - 14 * 86400))
    return {k: v for k, v in history.items()
            if k[:1].isdigit() and k >= cutoff and isinstance(v, dict)}


def save_history(history):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY_PATH.with_name(f"{HISTORY_PATH.name}.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        json.dump(history, f, separators=(",", ":"))
    os.chmod(tmp, 0o600)
    os.replace(tmp, HISTORY_PATH)


def record_usage(history, util_5h, util_7d=None):
    """Record the 5h peak for the current block and the day's 7d contribution."""
    if util_5h is None and util_7d is None:
        return history
    day = time.strftime("%Y-%m-%d")
    bucket = history.setdefault(day, {})
    if util_5h is not None:
        block = str(_current_block_index())
        if util_5h > bucket.get(block, 0.0):
            bucket[block] = round(util_5h, 6)
    if util_7d is not None:
        bucket.setdefault("7d_start", round(util_7d, 6))
        bucket["7d_latest"] = round(util_7d, 6)
    return _sanitize_history(history)


def load_state():
    try:
        with open(STATE_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    rate_data = data.get("rate_data")
    last = data.get("last_update_time")
    if not isinstance(rate_data, dict) or not isinstance(last, (int, float)):
        return {}
    if time.time() - float(last) > MAX_SNAPSHOT_AGE:
        return {}
    return {"rate_data": rate_data, "last_update_time": float(last)}


def save_state(rate_data, last_update_time):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(f"{STATE_PATH.name}.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        json.dump({"rate_data": rate_data, "last_update_time": last_update_time},
                  f, separators=(",", ":"))
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_time(ts):
    if ts is None:
        return "?"
    rem = max(0, ts - time.time())
    if rem == 0:
        return "now"
    d, rem = divmod(int(rem), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m or not parts:
        parts.append(f"{m}m")
    return " ".join(parts)


def bar_color(v):
    if v is None:
        return DIM
    if v < 0.50:
        return GREEN
    if v < 0.80:
        return YELLOW
    return RED


# ---------------------------------------------------------------------------
# App icon (generated once, pure stdlib, no Pillow)
# ---------------------------------------------------------------------------

def ensure_icon():
    if ICON_PATH.exists():
        return
    W = H = 64
    bg = (0x22, 0x1a, 0x15, 255)
    brd = (0x5a, 0x44, 0x30, 255)
    base_c = (0x3d, 0x2e, 0x22, 255)
    transp = (0, 0, 0, 0)
    bars_def = [
        ((0x8f, 0xac, 0x5f, 255), (0xa8, 0xc4, 0x7e, 255), 0.80),
        ((0xd4, 0xa9, 0x4c, 255), (0xe2, 0xbe, 0x6e, 255), 0.55),
        ((0xc8, 0x9b, 0x6e, 255), (0xd8, 0xb3, 0x8e, 255), 0.32),
    ]

    def in_rrect(x, y, x0, y0, x1, y1, r):
        if x < x0 or x > x1 or y < y0 or y > y1:
            return False
        if x < x0 + r and y < y0 + r:
            return (x - x0 - r) ** 2 + (y - y0 - r) ** 2 <= r * r
        if x > x1 - r and y < y0 + r:
            return (x - x1 + r) ** 2 + (y - y0 - r) ** 2 <= r * r
        if x < x0 + r and y > y1 - r:
            return (x - x0 - r) ** 2 + (y - y1 + r) ** 2 <= r * r
        if x > x1 - r and y > y1 - r:
            return (x - x1 + r) ** 2 + (y - y1 + r) ** 2 <= r * r
        return True

    bw, gap = 12, 5
    tw = 3 * bw + 2 * gap
    x0 = (W - tw) // 2
    bbot, btop = 50, 10
    bh = bbot - btop
    bars = [(x0 + i * (bw + gap), c, hi, int(bh * f)) for i, (c, hi, f) in enumerate(bars_def)]

    pixels = []
    for y in range(H):
        row = []
        for x in range(W):
            if not in_rrect(x, y, 0, 0, W - 1, H - 1, 10):
                row.append(transp)
            elif not in_rrect(x, y, 2, 2, W - 3, H - 3, 8):
                row.append(brd)
            else:
                px = bg
                for bx, bc, bchi, bht in bars:
                    by0 = bbot - bht
                    if bx <= x < bx + bw and by0 <= y <= bbot:
                        px = bchi if y == by0 else bc
                        break
                else:
                    if y == bbot + 1 and x0 <= x < x0 + tw:
                        px = base_c
                row.append(px)
        pixels.append(row)

    def chunk(ctype, data):
        c = ctype + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    raw = b""
    for pxrow in pixels:
        raw += b"\x00"
        for pr, pg, pb, pa in pxrow:
            raw += struct.pack("BBBB", pr, pg, pb, pa)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw))
           + chunk(b"IEND", b""))
    try:
        ICON_PATH.write_bytes(png)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# CLI commands (work without a display)
# ---------------------------------------------------------------------------

def _setup_hint():
    return (
        "No Claude login found.\n\n"
        "This widget reuses the token Claude Code stores when you log in.\n"
        "  1. Install Claude Code:  https://www.claude.com/product/claude-code\n"
        "  2. Run `claude` once and log in with your Claude Pro/Max account.\n"
        "  3. Re-run `claude-usage login`.\n\n"
        f"(Looking for: {CREDENTIALS_PATH})"
    )


def _print_usage_block(creds, data):
    print(f"  Plan:    Claude {plan_label(creds)}")
    rows = [
        ("5-hour", data.get("5h_util"), data.get("5h_reset")),
        ("7-day", data.get("7d_util"), data.get("7d_reset")),
    ]
    if data.get("sonnet_util") is not None:
        rows.append(("7-day Sonnet", data.get("sonnet_util"), data.get("sonnet_reset")))
    for name, util, reset in rows:
        if util is None:
            continue
        pct = util * 100
        filled = int(round(util * 20))
        bar = "█" * filled + "░" * (20 - filled)
        resets = f"resets in {fmt_time(reset)}" if reset else ""
        print(f"  {name:<13} {bar} {pct:5.1f}%   {resets}")
    if data.get("status") == "rejected":
        print("  ⚠ You are currently rate-limited.")


def cmd_login(_args):
    """Detect the Claude account, validate the token live, and print usage."""
    creds = read_credentials()
    if "error" in creds:
        if creds["error"] == "not-logged-in":
            print(_setup_hint())
            return 1
        print(f"Error: {creds['error']}")
        return 1

    print(f"✓ Found your Claude account ({plan_label(creds)})")
    if is_token_expired(creds):
        print("⚠ The stored token has expired. Open Claude Code (run `claude`) to "
              "refresh it, then try again.")
        return 1

    print("  Checking live usage…")
    data = fetch_usage(creds["access_token"])
    if "error" in data:
        print(f"  Could not fetch usage: {data['error']}")
        if data.get("http_code") == 429:
            print("  (Anthropic is rate-limiting this request. If you have Tor running, "
                  "the widget can auto-route around it.)")
        return 1
    print()
    _print_usage_block(creds, data)
    print("\nYou're all set. Run `claude-usage` to launch the widget.")
    return 0


def cmd_status(_args):
    """One-shot: print current usage and exit."""
    creds = read_credentials()
    if "error" in creds:
        print(_setup_hint() if creds["error"] == "not-logged-in" else f"Error: {creds['error']}")
        return 1
    if is_token_expired(creds):
        print("Token expired. Open Claude Code to refresh, then retry.")
        return 1
    data = fetch_usage(creds["access_token"])
    if "error" in data:
        print(f"Error: {data['error']}")
        return 1
    _print_usage_block(creds, data)
    return 0


# ---------------------------------------------------------------------------
# GUI (lazy tkinter import so the CLI works headless)
# ---------------------------------------------------------------------------

def launch_gui(_args=None):
    try:
        import tkinter as tk
    except ImportError:
        print("The desktop widget needs Tkinter, which isn't installed.\n"
              "On Debian/Ubuntu:  sudo apt install python3-tk\n"
              "Meanwhile you can use:  claude-usage status")
        return 1

    # -- compact horizontal bar: label [====   ] 42%  resets 2h 13m ----------
    class CompactBar(tk.Frame):
        def __init__(self, parent, label):
            super().__init__(parent, bg=BG)
            self.columnconfigure(1, weight=1)
            self._frac, self._color = 0.0, DIM
            tk.Label(self, text=label, font=(FONT, 10, "bold"), fg=TEXT_BOLD,
                     bg=BG, anchor="e", width=3).grid(row=0, column=0, padx=(0, 6), sticky="e")
            self._canvas = tk.Canvas(self, height=12, bg=BAR_BG, highlightthickness=0, bd=0)
            self._canvas.grid(row=0, column=1, sticky="nsew", padx=(0, 6))
            self._canvas.bind("<Configure>", lambda e: self._draw())
            self._pct = tk.Label(self, text="--%", font=(FONT, 11, "bold"), fg=DIM,
                                 bg=BG, anchor="e", width=6)
            self._pct.grid(row=0, column=2, sticky="e")
            self._info = tk.Label(self, text="", font=(FONT, 8), fg=DIM, bg=BG,
                                  anchor="e", width=13)
            self._info.grid(row=0, column=3, sticky="e", padx=(4, 0))

        def set(self, util, reset_ts=None, placeholder=None):
            if util is None:
                self._pct.config(text=placeholder or "--%", fg=DIM)
                self._info.config(text="")
                self._frac, self._color = 0.0, DIM
            else:
                c = bar_color(util)
                self._pct.config(text=f"{util * 100:.1f}%", fg=c)
                self._info.config(text=f"resets {fmt_time(reset_ts)}" if reset_ts else "")
                self._frac, self._color = util, c
            self._draw()

        def _draw(self):
            c = self._canvas
            c.delete("all")
            w, h = c.winfo_width(), c.winfo_height()
            if w <= 1:
                return
            c.create_rectangle(0, 0, w, h, fill=BAR_BG, outline="")
            fw = max(0, min(w, int(w * self._frac)))
            if fw > 0:
                c.create_rectangle(0, 0, fw, h, fill=self._color, outline="")

    # -- 7-day x 5-block heatmap --------------------------------------------
    class WeeklyHeatmap(tk.Frame):
        def __init__(self, parent):
            super().__init__(parent, bg=BG)
            tk.Label(self, text="Last 7 days", font=(FONT, 8, "bold"),
                     fg=DIM, bg=BG).pack(anchor="w", pady=(0, 1))
            self._canvas = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0, width=230)
            self._canvas.pack(fill=tk.BOTH, expand=True)
            self._history = {}
            self._canvas.bind("<Configure>", lambda e: self._draw())

        def update_data(self, history):
            self._history = history
            self._draw()

        def _draw(self):
            c = self._canvas
            c.delete("all")
            w, h = c.winfo_width(), c.winfo_height()
            if w <= 1 or h <= 1:
                return
            now = time.time()
            days = []
            for i in range(6, -1, -1):
                t = time.localtime(now - i * 86400)
                days.append((time.strftime("%Y-%m-%d", t), time.strftime("%a", t)[:2], i == 0))
            lbl_w, pct_w, gap = 22, 30, 2
            grid_w = w - lbl_w - pct_w - 4
            cell_w = max(4, (grid_w - 4 * gap) // 5)
            row_h = max(8, (h - 2) // 7 - 2)
            y = 1
            for day_key, day_lbl, is_today in days:
                blocks = self._history.get(day_key, {})
                c.create_text(lbl_w - 2, y + row_h // 2, text=day_lbl,
                              font=(FONT, 7, "bold") if is_today else (FONT, 7),
                              fill=TEXT_BOLD if is_today else DIM, anchor="e")
                x = lbl_w + 2
                for bi in range(5):
                    val = blocks.get(str(bi))
                    c.create_rectangle(x, y, x + cell_w, y + row_h, fill=BAR_BG, outline="")
                    if val and val > 0.001:
                        fw = max(1, int(cell_w * min(val, 1.0)))
                        c.create_rectangle(x, y, x + fw, y + row_h, fill=bar_color(val), outline="")
                    x += cell_w + gap
                start, latest = blocks.get("7d_start"), blocks.get("7d_latest")
                if start is not None and latest is not None:
                    contrib = max(0.0, latest - start)
                    c.create_text(x + 2, y + row_h // 2, text=f"{contrib * 100:.0f}%",
                                  font=(FONT, 7), fill=TEXT if contrib > 0.001 else DIM, anchor="w")
                y += row_h + 2

    # -- main window --------------------------------------------------------
    class App:
        def __init__(self):
            self.root = tk.Tk(className="claude_usage")
            self.root.title("Claude Usage")
            self.root.configure(bg=BG)
            self.root.geometry("640x192")
            self.root.minsize(560, 152)
            ensure_icon()
            try:
                self._icon = tk.PhotoImage(file=str(ICON_PATH))
                self.root.iconphoto(True, self._icon)
            except tk.TclError:
                pass

            self.always_on_top = tk.BooleanVar(value=False)
            self.interval = tk.IntVar(value=DEFAULT_INTERVAL)
            self.rate_data = {}
            self.last_update_time = None
            self.creds = {}
            self._history = load_history()
            self._timer = None
            self._tick_timer = None
            self._probe_in_flight = False
            self._closing = False
            self._cooldown_until = 0.0
            self._rate_limit_streak = 0
            self._use_tor = False
            self._retry_soon = False  # request a fast reschedule from _on_result's finally
            self._tor_supported = bool(shutil.which("torsocks") and shutil.which("curl"))

            self._build_ui()
            self.root.protocol("WM_DELETE_WINDOW", self._on_close)
            self._load_credentials()
            self._restore_cached_state()
            self._tick()
            self._run_probe()

        def _build_ui(self):
            pad = 8
            hdr = tk.Frame(self.root, bg=BG)
            hdr.pack(fill=tk.X, padx=pad, pady=(4, 0))
            self.plan_lbl = tk.Label(hdr, text="Claude", font=(FONT, 11, "bold"),
                                     fg=TEXT_BOLD, bg=BG)
            self.plan_lbl.pack(side=tk.LEFT)
            self.elapsed_lbl = tk.Label(hdr, text="", font=(FONT, 9), fg=DIM, bg=BG)
            self.elapsed_lbl.pack(side=tk.LEFT, padx=(8, 0))

            self.status_dot = tk.Label(hdr, text="", font=(FONT, 8), fg=DIM, bg=BG)
            self.status_dot.pack(side=tk.RIGHT)
            self.updated_lbl = tk.Label(hdr, text="", font=(FONT, 8), fg=DIM, bg=BG)
            self.updated_lbl.pack(side=tk.RIGHT, padx=(0, 6))

            tk.Frame(self.root, bg=SEP, height=1).pack(fill=tk.X, padx=pad, pady=(3, 2))

            body = tk.Frame(self.root, bg=BG)
            body.columnconfigure(0, weight=1)
            body.columnconfigure(1, weight=2)
            body.rowconfigure(0, weight=1)

            bars = tk.Frame(body, bg=BG)
            bars.grid(row=0, column=0, sticky="nsew")
            self.bar_5h = CompactBar(bars, "5h")
            self.bar_5h.pack(fill=tk.X, pady=(0, 2))
            self.bar_7d = CompactBar(bars, "7d")
            self.bar_7d.pack(fill=tk.X, pady=(0, 2))
            self.bar_sonnet = CompactBar(bars, "So")  # packed only on Max plans

            self.heatmap = WeeklyHeatmap(body)
            self.heatmap.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
            self.heatmap.update_data(self._history)

            # -- slim control bar (reserved at the bottom before body fills) --
            ctrl = tk.Frame(self.root, bg=BG_LIGHT)
            ctrl.pack(side=tk.BOTTOM, fill=tk.X, padx=pad, pady=(2, 4))
            self.ref_btn = tk.Button(ctrl, text="Refresh", font=(FONT, 7, "bold"),
                                     fg=TEXT_BOLD, bg=BTN_BG, activebackground=BTN_HOVER,
                                     activeforeground=TEXT_BOLD, bd=0, padx=6, pady=1,
                                     command=self._manual_refresh)
            self.ref_btn.pack(side=tk.LEFT, padx=(2, 0), pady=2)

            self.tor_btn = tk.Button(ctrl, text="Tor", font=(FONT, 7, "bold"),
                                     fg=DIM, bg=BTN_BG, activebackground=BTN_HOVER,
                                     activeforeground=DIM, bd=0, padx=6, pady=1,
                                     command=self._toggle_tor)
            self.tor_btn.pack(side=tk.LEFT, padx=(4, 0), pady=2)
            if not self._tor_supported:
                self.tor_btn.config(state=tk.DISABLED)

            tk.Checkbutton(ctrl, text="Pin", variable=self.always_on_top,
                           command=self._toggle_pin, font=(FONT, 7), fg=DIM, bg=BG_LIGHT,
                           selectcolor=BG, activebackground=BG_LIGHT,
                           activeforeground=TEXT_BOLD).pack(side=tk.LEFT, padx=(6, 0))

            tk.Label(ctrl, text="every", font=(FONT, 7), fg=DIM,
                     bg=BG_LIGHT).pack(side=tk.LEFT, padx=(8, 1))
            tk.Spinbox(ctrl, from_=MIN_INTERVAL, to=MAX_INTERVAL, increment=10,
                       textvariable=self.interval, width=4, font=(FONT, 7), bg=BG,
                       fg=TEXT_BOLD, buttonbackground=BG, insertbackground=TEXT_BOLD,
                       highlightthickness=0, bd=0, command=self._interval_changed
                       ).pack(side=tk.LEFT)
            tk.Label(ctrl, text="s", font=(FONT, 7), fg=DIM, bg=BG_LIGHT).pack(side=tk.LEFT)

            # body fills the middle, after the control bar has claimed the bottom
            body.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=pad)

            self.setup_lbl = tk.Label(self.root, text="", font=(FONT, 9), fg=YELLOW,
                                      bg=BG, justify=tk.LEFT, wraplength=560)

        # -- credential / state setup --
        def _load_credentials(self):
            self.creds = read_credentials()
            if "error" in self.creds:
                self._show_setup_message()
                return
            self.setup_lbl.pack_forget()  # clear any stale "not logged in" message
            self.plan_lbl.config(text=f"Claude {plan_label(self.creds)}", fg=TEXT_BOLD)
            if is_max_plan(self.creds):
                self.bar_sonnet.pack(fill=tk.X, pady=(0, 2))

        def _show_setup_message(self):
            self.plan_lbl.config(text="Not logged in", fg=YELLOW)
            self.setup_lbl.config(
                text="No Claude login found. Install Claude Code, run `claude` and log "
                     "in once, then reopen this widget (or run `claude-usage login`).")
            self.setup_lbl.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(2, 4))

        def _restore_cached_state(self):
            cached = load_state()
            if not cached:
                return
            self.rate_data = cached["rate_data"]
            self.last_update_time = cached["last_update_time"]
            self._update_bars()
            self.status_dot.config(text="cached", fg=DIM)

        # -- controls --
        def _toggle_pin(self):
            self.root.attributes("-topmost", self.always_on_top.get())

        def _interval_changed(self):
            try:
                v = max(MIN_INTERVAL, min(MAX_INTERVAL, int(self.interval.get())))
                self.interval.set(v)
            except (ValueError, tk.TclError):
                self.interval.set(DEFAULT_INTERVAL)

        def _manual_refresh(self):
            self._cooldown_until = 0.0
            self._cancel_timer()
            self._run_probe()

        def _toggle_tor(self):
            if not self._tor_supported:
                return
            self._use_tor = not self._use_tor
            self._rate_limit_streak = 0
            self._cooldown_until = 0.0
            self.tor_btn.config(text="Tor ON" if self._use_tor else "Tor",
                                fg=GREEN if self._use_tor else DIM)
            self._cancel_timer()
            self._run_probe()

        # -- timers --
        def _cancel_timer(self):
            if self._timer:
                try:
                    self.root.after_cancel(self._timer)
                except tk.TclError:
                    pass
                self._timer = None

        def _schedule_after(self, delay_s):
            self._cancel_timer()
            self._timer = self.root.after(max(1000, int(delay_s * 1000)), self._run_probe)

        def _effective_interval(self):
            try:
                return max(MIN_INTERVAL, min(MAX_INTERVAL, int(self.interval.get())))
            except (ValueError, tk.TclError):
                return DEFAULT_INTERVAL

        def _tick(self):
            """1 Hz UI refresh: 'updated Ns ago' + live reset countdowns."""
            if self._closing:
                return
            now = time.time()
            if self.last_update_time:
                e = int(now - self.last_update_time)
                stale = e >= STALE_AFTER_SECONDS
                in_cd = self._cooldown_until > now
                if in_cd:
                    label = f"cooldown {int(self._cooldown_until - now)}s"
                elif e < 60:
                    label = f"{e}s ago"
                else:
                    label = f"{e // 60}m ago"
                self.updated_lbl.config(text=label, fg=YELLOW if (stale or in_cd) else DIM)
            if self.rate_data:
                self._update_bars()
            self._tick_timer = self.root.after(1000, self._tick)

        # -- probe loop --
        def _run_probe(self):
            if self._closing:
                return
            now = time.time()
            if now < self._cooldown_until:
                self._schedule_after(self._cooldown_until - now)
                return
            if self._probe_in_flight:
                self._schedule_after(1)
                return
            self.creds = read_credentials()
            if "error" in self.creds:
                self._show_setup_message()
                self._schedule_after(self._effective_interval())
                return
            if self.setup_lbl.winfo_ismapped():
                self._load_credentials()  # creds readable again, restore the normal UI
            if is_token_expired(self.creds):
                self.status_dot.config(text="token expired", fg=YELLOW)
                self._schedule_after(max(300, self._effective_interval()))
                return
            self._probe_in_flight = True
            token = self.creds["access_token"]
            threading.Thread(target=self._probe_bg, args=(token,), daemon=True).start()

        def _probe_bg(self, token):
            try:
                result = fetch_usage(token, use_tor=self._use_tor)
            except Exception as e:
                result = {"error": str(e)}
            if self._closing:
                return
            try:
                self.root.after(0, self._on_result, result)
            except tk.TclError:
                self._closing = True

        def _on_result(self, result):
            try:
                if self._closing:
                    return
                if "error" in result:
                    self._handle_error(result)
                else:
                    self._handle_success(result)
            finally:
                self._probe_in_flight = False
                if self._closing:
                    return
                if self._retry_soon:
                    self._retry_soon = False
                    wait = 2  # fast retry, e.g. right after auto-switching to Tor
                else:
                    wait = self._effective_interval()
                    if self._cooldown_until > time.time():
                        wait = max(wait, int(self._cooldown_until - time.time()))
                self._schedule_after(wait)

        def _handle_error(self, result):
            if result.get("http_code") == 429:
                self._rate_limit_streak += 1
                # After persistent 429s on the direct connection, auto-route via Tor.
                if (not self._use_tor and self._tor_supported and tor_available()
                        and self._rate_limit_streak >= TOR_FALLBACK_429_THRESHOLD):
                    self._use_tor = True
                    self.tor_btn.config(text="Tor ON", fg=GREEN)
                    self.status_dot.config(text="429 → Tor", fg=ACCENT)
                    self._rate_limit_streak = 0
                    self._cooldown_until = 0.0
                    self._retry_soon = True  # _on_result's finally schedules the fast Tor retry
                    return
                if self._use_tor and self._rate_limit_streak >= TOR_GIVE_UP_429_THRESHOLD:
                    self._use_tor = False
                    self.tor_btn.config(text="Tor", fg=DIM)
                    self.status_dot.config(text="Tor exhausted", fg=RED)
                    self._rate_limit_streak = 0
                wait = result.get("retry_after")
                if not wait or self._rate_limit_streak >= 3:
                    wait = min(RATE_LIMIT_MAX_BACKOFF,
                               RATE_LIMIT_BASE_BACKOFF * (2 ** (self._rate_limit_streak - 1)))
                self._cooldown_until = time.time() + wait
                self.status_dot.config(text=f"HTTP 429 · wait {int(wait)}s", fg=YELLOW)
            else:
                self._rate_limit_streak = 0
                self.status_dot.config(text=result["error"][:40], fg=RED)
            if self.rate_data:
                self._update_bars()

        def _handle_success(self, result):
            self._cooldown_until = 0.0
            self._rate_limit_streak = 0
            via = result.get("via")
            if via in ("direct", "tor"):
                self._use_tor = (via == "tor")
                if self._tor_supported:
                    self.tor_btn.config(text="Tor ON" if self._use_tor else "Tor",
                                        fg=GREEN if self._use_tor else DIM)
            self.rate_data = result
            self.last_update_time = time.time()
            self._update_bars()
            if result.get("status") == "rejected":
                self.status_dot.config(text="● limited", fg=RED)
            elif self._use_tor:
                self.status_dot.config(text="● ok (Tor)", fg=GREEN)
            else:
                self.status_dot.config(text="● ok", fg=GREEN)
            self._history = record_usage(self._history, result.get("5h_util"),
                                         result.get("7d_util"))
            save_history(self._history)
            save_state(self.rate_data, self.last_update_time)
            self.heatmap.update_data(self._history)

        def _update_bars(self):
            d = self.rate_data
            self.bar_5h.set(d.get("5h_util"), d.get("5h_reset"))
            self.bar_7d.set(d.get("7d_util"), d.get("7d_reset"))
            if is_max_plan(self.creds):
                self.bar_sonnet.set(d.get("sonnet_util"), d.get("sonnet_reset"), placeholder="--")
            reset_ts = d.get("7d_reset")
            if reset_ts:
                remaining = max(0, reset_ts - time.time())
                elapsed = max(0, min(100, (1.0 - remaining / (7 * 86400)) * 100))
                self.elapsed_lbl.config(text=f"week {elapsed:.0f}% elapsed",
                                        fg=bar_color(d.get("7d_util")))
            else:
                self.elapsed_lbl.config(text="")

        def _on_close(self):
            self._closing = True
            self._cancel_timer()
            if self._tick_timer:
                try:
                    self.root.after_cancel(self._tick_timer)
                except tk.TclError:
                    pass
            self.root.destroy()

        def run(self):
            self.root.mainloop()

    App().run()
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="claude-usage",
        description="A tiny desktop widget for your Claude Pro/Max rate limits.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("login", help="Detect your Claude account and print live usage.")
    sub.add_parser("status", help="Print current usage once and exit (no window).")
    sub.add_parser("gui", help="Launch the desktop widget (default).")

    args = parser.parse_args(argv)
    if args.command == "login":
        return cmd_login(args)
    if args.command == "status":
        return cmd_status(args)
    return launch_gui(args)


if __name__ == "__main__":
    sys.exit(main())
