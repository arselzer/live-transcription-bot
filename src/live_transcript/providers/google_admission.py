"""Google admission states and scoped diagnostics, independent of capture/ASR."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from live_transcript.core.files import atomic_json
from live_transcript.core.options import provider_origin
from live_transcript.providers.interaction import NativeInputError, NativeTargetChanged
from live_transcript.runtime.console import operator_control_active

SNAPSHOT_JS = """includeText => {
  const visible=e=>!!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
  const label=e=>(e.getAttribute('aria-label')||e.getAttribute('title')||e.innerText||'').trim().slice(0,160);
  const controls=root=>[...root.querySelectorAll('button,[role=button]')].filter(visible).slice(0,150).map(label);
  return {controls:controls(document), locale:document.documentElement.lang||'',
    join_controls:[...document.querySelectorAll('button,[role=button]')].filter(visible)
      .map(e=>(e.innerText||'').trim()).filter(t=>/^(Join now|Ask to join)$/i.test(t)),
    notices:[...document.querySelectorAll('[role=dialog],[role=alertdialog]')].filter(visible)
      .slice(0,10).map(e=>({text:e.innerText.slice(0,1600),controls:controls(e)})),
    statuses:[...document.querySelectorAll('h1,h2,[role=heading],[role=status]')].filter(visible)
      .slice(0,40).map(e=>e.innerText.trim().slice(0,500)),
    name_field:[...document.querySelectorAll('input:not([type=email]):not([type=password])')]
      .some(e=>visible(e) && (e.maxLength===60 || /name/i.test(e.getAttribute('aria-label')||e.placeholder||''))),
    challenge:[...document.querySelectorAll('iframe')].some(e=>{
      const r=e.getBoundingClientRect();return visible(e)&&r.width>100&&r.height>100&&
        /recaptcha.*(?:bframe|challenge)/i.test(e.src+' '+e.title);}),
    body:includeText ? document.body.innerText.slice(0,6000) : ''};
}"""
WAITING = re.compile(
    r"asking to be let in|waiting for (?:the )?host|you(?:'|’)ll join the call when|you're in the waiting room|please wait until a meeting host",
    re.I,
)
KNOWN_NOTICE = re.compile(
    r"(?:Gemini|someone|.+) is taking notes|this (?:meeting|call) is being recorded|your call audio and video will be shared with",
    re.I,
)
HOST_DENIAL = re.compile(
    r"denied your request|your request to join was denied|you (?:weren't|weren’t) allowed to join",
    re.I,
)
ACCESS_DENIAL = re.compile(
    r"you (?:can't|can’t|cannot) join this (?:video call|meeting|call)", re.I
)
RPC_PATH = re.compile(
    r"^/\$rpc/google\.rtc\.meetings\.v1\.(?:MeetingSpaceService/ResolveMeetingSpace|MeetingDeviceService/CreateMeetingDevice)$"
)


class AdmissionRefused(RuntimeError):
    def __init__(self, message, reason="google_access_denied"):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class AdmissionState:
    stage: str
    reason: str = ""


def label(value):
    return re.sub(r"\s*\([^)]{1,80}\)$", "", value.strip()).strip().lower()


def visible(locator):
    return locator.first.is_visible()


def button(page, text):
    pattern = re.compile(r"^" + re.escape(text) + r"(?:\s*\([^)]{1,80}\))?$", re.I)
    exact = page.get_by_role("button", name=pattern)
    if text in ("Join now", "Ask to join") and exact.count() == 0:
        # Meet can append accessibility guidance to the button name. Require
        # the exact visible text as well, so unrelated permission/consent
        # controls can never become a fallback target.
        return (
            page.get_by_role("button", name=re.compile(r"^" + re.escape(text) + r"(?:\s|$)", re.I))
            .filter(has_text=re.compile(r"^\s*" + re.escape(text) + r"\s*$", re.I))
            .first
        )
    return exact.first


def click_if_visible(page, text):
    if operator_control_active():
        return False
    try:
        control = button(page, text)
        if control.is_visible() and control.is_enabled():
            control.click(timeout=2500)
            return True
    except Exception:
        pass
    return False


def inspect(page, include_text=True):
    data = page.evaluate(SNAPSHOT_JS, include_text)
    if not isinstance(data, dict):
        raise ValueError("Google UI observation is unavailable")
    return data


def classify(data, *, require_sign_in=False, url="", refusal=None):
    controls = {label(value) for value in data.get("controls", []) if isinstance(value, str)}
    controls.update(
        label(value)
        for value in data.get("join_controls", [])
        if isinstance(value, str) and label(value) in ("join now", "ask to join")
    )
    if isinstance(url, str) and url and not provider_origin("google", url):
        return AdmissionState(
            "operator",
            "google_auth_session_missing"
            if urlsplit(url).hostname == "accounts.google.com"
            else "google_navigation_unexpected",
        )
    body = data.get("body", "")
    # Status and dialog copy, rather than arbitrary participant chat, gates entry.
    if data.get("challenge"):
        return AdmissionState("operator", "captcha_required")
    for notice in data.get("notices", []):
        text = notice.get("text", "")
        if KNOWN_NOTICE.search(text):
            if any(label(v) == "join now" for v in notice.get("controls", [])):
                return AdmissionState("join_notice")
            return AdmissionState("operator", "google_consent_required")
        if re.search(r"consent|agree|start taking notes", text, re.I):
            return AdmissionState("operator", "google_consent_required")
    waiting = any(WAITING.search(value) for value in data.get("statuses", []))
    in_call = "leave call" in controls and bool(
        controls & {"present now", "share screen", "show everyone", "people", "participants"}
    )
    if in_call and not waiting and not controls & {"join now", "ask to join"}:
        return AdmissionState("admitted")
    if HOST_DENIAL.search(body):
        return AdmissionState("rejected", "host_denied")
    if isinstance(refusal, dict) and refusal.get("status") == 403:
        return AdmissionState("rejected", "google_join_refused")
    if ACCESS_DENIAL.search(body):
        return AdmissionState("rejected", "google_access_denied")
    if re.search(r"no one responded to your request to join", body, re.I):
        return AdmissionState("rejected", "google_admission_timeout")
    if waiting:
        return AdmissionState("waiting")
    if "switch here" in controls:
        return AdmissionState("operator", "google_account_in_use")
    if require_sign_in and (
        data.get("name_field") or urlsplit(url).hostname == "accounts.google.com"
    ):
        return AdmissionState("operator", "google_auth_session_missing")
    if re.search(r"(?:sign in|log in) to join", body, re.I):
        return AdmissionState("operator", "google_sign_in_required")
    if data.get("name_field") or controls & {"join now", "ask to join"}:
        return AdmissionState("prejoin")
    return AdmissionState("loading")


def joined(page):
    try:
        return classify(inspect(page, False), url=page.url).stage == "admitted"
    except Exception:
        return False


def navigation_url(url):
    """UI language is an adapter contract; spoken-language recognition is separate."""
    p = urlsplit(url)
    query = [
        (key, value) for key, value in parse_qsl(p.query, keep_blank_values=True) if key != "hl"
    ]
    query.append(("hl", "en"))
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(query), p.fragment))


def watch_refusals(page):
    page._lt_google_refusal = None

    def response_received(response):
        try:
            parsed = urlsplit(response.url)
            method = parsed.path.rsplit("/", 1)[-1]
            if (
                provider_origin("google", response.url)
                and RPC_PATH.fullmatch(parsed.path)
                and response.status == 403
            ):
                # No request payloads, query parameters, tokens or cookies enter diagnostics.
                page._lt_google_refusal = dict(status=403, method=method)
        except Exception:
            pass

    page.on("response", response_received)


def checkpoint(page, root, state, data):
    if root is None:
        return
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = dict(
        utc=time.time(),
        stage=state.stage,
        reason=state.reason,
        locale=data.get("locale", ""),
        controls=data.get("controls", [])[:40],
    )
    refusal = getattr(page, "_lt_google_refusal", None)
    if isinstance(refusal, dict):
        info["rpc"] = refusal
    atomic_json(root / "state.json", info)
    fd = os.open(
        root / "transitions.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(fd, "a") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(json.dumps(info) + "\n")
    try:
        screenshot = root / (str(time.time_ns()) + "-" + state.stage + ".jpg")
        page.screenshot(path=str(screenshot), type="jpeg", timeout=1500)
        screenshot.chmod(0o600)
    except Exception:
        pass


def prejoin(page, name, timeout=330, *, interaction=None, require_sign_in=False, diagnostics=None):
    deadline = time.monotonic() + timeout
    submitted = False
    entered = False
    previous = None
    handled_notices = set()

    def click(text, *, scope=None):
        source = scope or page
        if interaction is None:
            return click_if_visible(source, text)
        control = button(source, text)
        if control.is_visible() and control.is_enabled():
            interaction.click(control)
            return True
        return False

    while time.monotonic() < deadline:
        try:
            # Positive toolbar evidence prevents meeting chat becoming a denial.
            if joined(page):
                if diagnostics is not None:
                    checkpoint(page, diagnostics, AdmissionState("admitted"), inspect(page, False))
                return
            if operator_control_active():
                page.wait_for_timeout(500)
                continue
            data = inspect(page)
            state = classify(
                data,
                require_sign_in=require_sign_in,
                url=page.url,
                refusal=getattr(page, "_lt_google_refusal", None),
            )
            if state != previous:
                checkpoint(page, diagnostics, state, data)
                print("GOOGLE_ADMISSION", state.stage, state.reason, flush=True)
                previous = state
            if state.stage in ("rejected", "operator"):
                raise AdmissionRefused(
                    "Google admission requires operator action: " + state.reason, state.reason
                )
            if state.stage == "join_notice":
                for dialog in (
                    page.get_by_role("dialog").all() + page.get_by_role("alertdialog").all()
                ):
                    if dialog.is_visible():
                        text = dialog.inner_text()
                        if text not in handled_notices and KNOWN_NOTICE.search(text):
                            if click("Join now", scope=dialog):
                                handled_notices.add(text)
                                break
            elif state.stage == "prejoin" and not submitted:
                # Recording listens to playback, not the microphone. Meet may
                # put this media setup dialog over an otherwise visible form.
                # Clear that known dialog before targeting the underlying name.
                controls = {label(value) for value in data.get("controls", [])}
                if "continue without microphone" in controls and click(
                    "Continue without microphone"
                ):
                    page.wait_for_timeout(250)
                    continue
                click("Got it")
                field = page.get_by_role("textbox").first
                if field.is_visible() and (not entered or field.input_value() == ""):
                    if interaction is None:
                        field.fill(name)
                        field.press("Tab")
                    else:
                        interaction.fill_name(field, name)
                    entered = True
                click("Turn off microphone")
                submitted = click("Join now") or click("Ask to join")
            page.wait_for_timeout(500)
        except NativeTargetChanged:
            print(
                "GOOGLE_INPUT target changed before click; rechecking admission state", flush=True
            )
            page.wait_for_timeout(250)
            continue
        except NativeInputError as exc:
            # A late RPC refusal can remove the control while native input is
            # verifying it. Preserve the provider's decision as the primary cause.
            try:
                latest = inspect(page)
                verdict = classify(
                    latest,
                    require_sign_in=require_sign_in,
                    url=page.url,
                    refusal=getattr(page, "_lt_google_refusal", None),
                )
                if verdict.stage == "admitted":
                    checkpoint(page, diagnostics, verdict, latest)
                    return
                if verdict.stage in ("rejected", "operator"):
                    checkpoint(page, diagnostics, verdict, latest)
                    raise AdmissionRefused(
                        "Google admission requires operator action: " + verdict.reason,
                        verdict.reason,
                    ) from exc
            except AdmissionRefused:
                raise
            except Exception:
                pass
            try:
                if diagnostics is not None:
                    cause = exc
                    while cause.__cause__ is not None:
                        cause = cause.__cause__
                    detail = re.sub(
                        r"https?://[^\s\"']+",
                        lambda match: urlunsplit((*urlsplit(match[0])[:3], "", "")),
                        str(cause),
                    )[:3000]
                    atomic_json(
                        Path(diagnostics) / "input-error.json",
                        dict(type=type(cause).__name__, detail=detail),
                    )
                checkpoint(
                    page,
                    diagnostics,
                    AdmissionState("operator", "google_input_unavailable"),
                    inspect(page),
                )
            except Exception:
                pass
            raise AdmissionRefused(str(exc), "google_input_unavailable") from exc
        except Exception as exc:
            if any(
                value in str(exc)
                for value in (
                    "Execution context was destroyed",
                    "Cannot find context",
                    "Frame was detached",
                )
            ):
                page.wait_for_timeout(300)
                continue
            raise
    raise AdmissionRefused(
        f"No confirmed admission within {timeout:g} seconds; no recording started",
        "google_admission_timeout",
    )
