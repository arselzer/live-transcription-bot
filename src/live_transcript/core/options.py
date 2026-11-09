"""Shared, offline validation for Zoom/Proton launch and recovery options."""

from pathlib import Path
import re
from urllib.parse import parse_qs, urlencode, urlsplit

PROVIDERS = ("zoom", "proton", "google", "jitsi")


def provider_origin(provider, url):
    """Allow UI/evidence access only on the selected provider's HTTPS origin."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or provider != "jitsi"
            and port not in (None, 443)
        ):
            return False
        host = parsed.hostname or ""
    except ValueError:
        return False
    if provider == "jitsi":
        from live_transcript.providers.jitsi import is_jitsi_url

        return port != 0 and is_jitsi_url(url)
    if provider == "zoom":
        return host == "zoom.us" or host.endswith(".zoom.us")
    return host == {"proton": "meet.proton.me", "google": "meet.google.com"}.get(
        provider, ""
    ) and bool(host)


def zoom_invitation(value):
    if re.fullmatch(r"[\d\s-]+", value):
        ident = re.sub(r"\D", "", value)
        if not 9 <= len(ident) <= 12:
            raise ValueError("Zoom meeting ID must contain 9–12 digits")
        return f"https://zoom.us/wc/{ident}/join", ident
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or not (host == "zoom.us" or host.endswith(".zoom.us"))
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Expected an HTTPS Zoom invitation or numeric meeting ID")
    match = re.search(r"/(?:j|s|wc/(?:join/)?)/*(\d{9,12})(?:/|$)", parsed.path)
    if match:
        ident = match[1]
        password = parse_qs(parsed.query).get("pwd", [])
        query = "?" + urlencode({"pwd": password[0]}) if password else ""
        return f"https://zoom.us/wc/{ident}/join{query}", ident
    if re.fullmatch(r"/my/[A-Za-z0-9._-]+/?", parsed.path):
        return value, ""
    raise ValueError("Unsupported Zoom invitation path")


def validate_output(path):
    path = Path(path).expanduser().resolve()
    if (
        path.suffix != ".wav"
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", path.stem)
        or any(c.isspace() for c in str(path))
    ):
        raise ValueError("Recording output needs a simple filename ending .wav, without whitespace")
    if path.exists() or path.with_suffix(".archive").exists():
        raise FileExistsError("Refusing to overwrite a previous recording: " + str(path))
    return path


def rejoin_options(args):
    """Normalize both option forms; remove outputs/dry-run before a new session."""
    result = {}
    i = 0
    while i < len(args):
        key, sep, value = args[i].partition("=")
        if key == "--dry":
            i += 1
            continue
        if key not in ("--name", "--max-min", "--passcode", "--out"):
            raise ValueError("Unknown meeting option: " + key)
        if not sep:
            i += 1
            if i == len(args):
                raise ValueError("Missing option value: " + key)
            value = args[i]
        if key != "--out":
            result[key] = value
        i += 1
    budget = int(result.get("--max-min", "180"))
    if not 1 <= budget <= 480:
        raise ValueError("Meeting budget must be between 1 and 480 minutes")
    return result, budget
