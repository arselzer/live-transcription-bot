"""Verified X11 input for a Google join form in the owned virtual display.

Targets come from browser locators. Pointer events calibrate viewport coordinates
against the X server; the target is checked again before any click or typing.
No motion recordings, fingerprint patches, or upstream implementation are used.
"""

import math
from contextlib import contextmanager
import os
import re
import shutil
import subprocess
import time

from live_transcript.core.options import provider_origin

POINTER_JS = """() => {
  if (!window.__ltNativeListening) {
    addEventListener('mousemove', e => {
      window.__ltNativePointer = {x:e.clientX,y:e.clientY,trusted:e.isTrusted};
    }, {passive:true});
    window.__ltNativeListening = true;
  }
  window.__ltNativePointer = null;
  return {x:screenX,y:screenY,w:outerWidth,h:outerHeight,
          innerW:innerWidth,innerH:innerHeight,dpr:devicePixelRatio};
}"""
HIT_JS = """(el, point) => {
  const p=window.__ltNativePointer;
  const top=document.elementFromPoint(point.x,point.y);
  const r=el.getBoundingClientRect();
  return !!p?.trusted && Math.abs(p.x-point.x)<2 && Math.abs(p.y-point.y)<2 &&
    p.x>=r.left && p.x<r.right && p.y>=r.top && p.y<r.bottom &&
    (top===el || el.contains(top)) && !el.disabled && el.getAttribute('aria-disabled')!=='true';
}"""


class NativeInputError(RuntimeError):
    """The selected control could not be reached safely; no fallback click."""


class NativeTargetChanged(NativeInputError):
    """Google replaced a target before any click; inspect the page again."""


class NativeInput:
    def __init__(self, page):
        self.page = page
        self.executable = shutil.which("xdotool")
        self.clipboard_tool = shutil.which("xclip")
        self.display = os.environ.get("DISPLAY")
        if not self.executable or not self.clipboard_tool or not self.display:
            raise NativeInputError(
                "Google native input requires xdotool, xclip and an owned X display"
            )
        self.run("getdisplaygeometry")

    def run(self, *arguments):
        try:
            return subprocess.run(
                [self.executable, *map(str, arguments)],
                env=dict(os.environ, DISPLAY=self.display),
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise NativeInputError("Native Google input failed: " + type(exc).__name__) from exc

    def trusted_page(self):
        if not provider_origin("google", self.page.url):
            raise NativeInputError("Native input is confined to the Google Meet page")

    @staticmethod
    def root_point(pointer, viewport, ratio, target):
        """Translate CSS coordinates using an observed X11-to-viewport mapping."""
        values = [*pointer, *viewport, ratio, *target]
        if not all(math.isfinite(value) for value in values) or not 0 < ratio <= 8:
            raise NativeInputError("Invalid browser coordinate observation")
        return tuple(
            round(raw + (wanted - client) * ratio)
            for raw, client, wanted in zip(pointer, viewport, target)
        )

    def move_to(self, control):
        self.trusted_page()
        self.page.bring_to_front()
        control.scroll_into_view_if_needed(timeout=2000)
        if not control.is_visible() or not control.is_enabled():
            raise NativeInputError("Join control is unavailable")
        dimensions = self.run("getdisplaygeometry").split()
        if len(dimensions) != 2:
            raise NativeInputError("Invalid X display geometry")
        width, height = map(int, dimensions)
        geometry = self.page.evaluate(POINTER_JS)
        ratio = float(geometry["dpr"])
        probe_x = round((geometry["x"] + geometry["w"] / 2) * ratio)
        probe_y = round((geometry["y"] + geometry["h"] - geometry["innerH"] / 2) * ratio)
        if not (1 <= probe_x < width - 1 and 1 <= probe_y < height - 1):
            raise NativeInputError("Browser viewport is outside its owned display")
        # A one-pixel move ensures an event even when the pointer was already here.
        self.run("mousemove", "--sync", probe_x + 1, probe_y)
        self.run("mousemove", "--sync", probe_x, probe_y)
        # wait_for_function creates a Function from a string, which Google's
        # Trusted Types policy rejects. Direct CDP reads need no unsafe eval.
        deadline = time.monotonic() + 1.5
        pointer = None
        while time.monotonic() < deadline:
            pointer = self.page.evaluate("() => window.__ltNativePointer")
            if isinstance(pointer, dict) and pointer.get("trusted") is True:
                break
            self.page.wait_for_timeout(40)
        else:
            raise NativeInputError("No verified native pointer event reached the browser")
        raw = self.run("getmouselocation", "--shell")
        match_x = re.search(r"^X=(\d+)$", raw, re.M)
        match_y = re.search(r"^Y=(\d+)$", raw, re.M)
        if not match_x or not match_y:
            raise NativeInputError("X pointer position unavailable")
        box = control.bounding_box()
        if not box or box["width"] <= 0 or box["height"] <= 0:
            raise NativeInputError("Join control has no visible rectangle")
        target = dict(x=box["x"] + box["width"] / 2, y=box["y"] + box["height"] / 2)
        x, y = self.root_point(
            (int(match_x[1]), int(match_y[1])),
            (pointer["x"], pointer["y"]),
            ratio,
            (target["x"], target["y"]),
        )
        if not (0 <= x < width and 0 <= y < height):
            raise NativeInputError("Join control is outside the owned display")
        self.run("mousemove", "--sync", x, y)
        self.page.wait_for_timeout(80)
        self.trusted_page()
        if not control.evaluate(HIT_JS, target, timeout=2000):
            raise NativeInputError("Pointer did not reach the original join control; click refused")
        return target

    def click(self, control):
        try:
            target = self.move_to(control)
            raw = self.run("getmouselocation", "--shell")
            window = re.search(r"^WINDOW=(\d+)$", raw, re.M)
            if not window or int(window[1]) <= 1:
                raise NativeInputError("The verified browser window has no X input target")
            # Chrome may report a renderer child as the pointer's X window.
            # Waiting for X input focus to equal that child can never complete,
            # even when the page already has focus. Verify the page instead.
            if self.page.evaluate("() => document.hasFocus()") is not True:
                self.run("windowfocus", window[1])
                deadline = time.monotonic() + 1
                while self.page.evaluate("() => document.hasFocus()") is not True:
                    if time.monotonic() >= deadline:
                        raise NativeInputError("The verified meeting page did not receive focus")
                    self.page.wait_for_timeout(50)
            self.trusted_page()
            if not control.evaluate(HIT_JS, target, timeout=2000):
                raise NativeInputError("Join control changed while focusing its window")
            self.run("click", "--clearmodifiers", 1)
        except NativeInputError:
            raise
        except Exception as exc:
            if type(exc).__name__ == "TimeoutError" or any(
                value in str(exc).lower()
                for value in (
                    "not attached to the dom",
                    "element is detached",
                    "execution context was destroyed",
                )
            ):
                raise NativeTargetChanged(
                    "Google replaced the control before native input"
                ) from exc
            raise NativeInputError(
                "Native target verification failed: " + type(exc).__name__
            ) from exc

    @contextmanager
    def clipboard(self, text):
        # Foreground ownership avoids xclip's default detached child inheriting
        # pipes. The helper lives only until the verified paste has completed.
        try:
            process = subprocess.Popen(
                [self.clipboard_tool, "-selection", "clipboard", "-in", "-quiet"],
                env=dict(os.environ, DISPLAY=self.display),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise NativeInputError("Private clipboard helper unavailable") from exc
        try:
            process.stdin.write(text.encode("utf-8"))
            process.stdin.close()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise NativeInputError("Clipboard helper stopped before name entry")
                try:
                    result = subprocess.run(
                        [self.clipboard_tool, "-selection", "clipboard", "-out"],
                        env=dict(os.environ, DISPLAY=self.display),
                        capture_output=True,
                        text=True,
                        timeout=0.3,
                        check=True,
                    )
                    if result.stdout == text:
                        break
                except (OSError, subprocess.SubprocessError):
                    pass
                self.page.wait_for_timeout(50)
            else:
                raise NativeInputError("Private name clipboard did not become ready")
            yield
        except (OSError, subprocess.SubprocessError) as exc:
            raise NativeInputError("Private name clipboard failed") from exc
        finally:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

    def verify_name(self, control, name):
        # XTEST queues events; Chrome may not have consumed the last key yet.
        deadline = time.monotonic() + 2
        while control.input_value() != name and time.monotonic() < deadline:
            self.page.wait_for_timeout(50)
        if control.input_value() != name:
            raise NativeInputError("Google participant name entry could not be verified")

    def fill_name(self, control, name):
        if not name.strip() or len(name) > 60 or not name.isprintable():
            raise NativeInputError("Google participant name must contain 1–60 printable characters")
        self.click(control)
        if not control.evaluate("el => document.activeElement === el"):
            raise NativeInputError("Google name field did not receive keyboard focus")
        self.run("key", "--clearmodifiers", "ctrl+a")
        if name.isascii():
            self.run("type", "--clearmodifiers", "--delay", 25, "--", name)
            self.verify_name(control, name)
        else:
            # xdotool's keysym remapping can change Unicode case on a US layout.
            # Paste exact UTF-8 through the owned display, then clear ownership.
            with self.clipboard(name):
                self.run("key", "--clearmodifiers", "ctrl+v")
                self.verify_name(control, name)
        self.run("key", "--clearmodifiers", "Tab")
