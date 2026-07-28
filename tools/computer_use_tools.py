"""Computer Use tools — screen capture, mouse, keyboard, clipboard for macOS."""

from __future__ import annotations

import base64
import logging
import platform
import time
from typing import Any

from .base import BaseTool

logger = logging.getLogger(__name__)

_PLATFORM_OK = platform.system() == "Darwin"
if _PLATFORM_OK:
    try:
        import Quartz
        from ApplicationServices import (
            AXIsProcessTrusted,
            AXIsProcessTrustedWithOptions,
        )
        from Quartz import (
            CGEventCreateKeyboardEvent,
            CGEventCreateMouseEvent,
            CGEventCreateScrollWheelEvent,
            CGEventGetLocation,
            CGEventPost,
            CGEventSourceCreate,
            CGEventSourceStateID,
            CGMouseButton,
            CGScrollEventUnit,
            kCGEventFlagMaskCommand,
            kCGEventFlagMaskControl,
            kCGEventFlagMaskShift,
            kCGEventKeyDown,
            kCGEventKeyUp,
            kCGEventLeftMouseDown,
            kCGEventLeftMouseUp,
            kCGEventMouseMoved,
            kCGEventOtherMouseDown,
            kCGEventOtherMouseUp,
            kCGEventRightMouseDown,
            kCGEventRightMouseUp,
            kCGHIDEventTap,
            kCGWindowImageDefault,
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
        )
        _QUARTZ_OK = True
    except ImportError:
        _QUARTZ_OK = False
        logger.warning("Quartz framework not available, Computer Use tools will be disabled")
else:
    _QUARTZ_OK = False


def _check_accessibility() -> str:
    if not _PLATFORM_OK:
        return "Error: Computer Use tools only work on macOS"
    if not _QUARTZ_OK:
        return "Error: Quartz framework not available (install pyobjc-framework-Quartz)"
    try:
        if not AXIsProcessTrusted():
            prompt = Quartz.CFDictionaryCreate(
                None, [Quartz.kAXTrustedCheckOptionPrompt], [True], 1
            )
            AXIsProcessTrustedWithOptions(prompt)
            return "Error: Accessibility permission not granted. Please grant in System Settings > Privacy & Security > Accessibility"
    except Exception as e:
        logger.warning("Accessibility check failed: %s", e)
    return ""


def _get_event_source() -> Any:
    return CGEventSourceCreate(CGEventSourceStateID.kCGEventSourceStateHIDSystemState)


class ScreenCaptureTool(BaseTool):
    name = "screen_capture"
    description = "Capture a screenshot of the entire screen or a specific region. Returns a base64-encoded PNG image."
    parameters = {
        "x": {
            "type": "integer",
            "description": "Left edge x coordinate (default: 0 for full screen)",
            "default": 0,
        },
        "y": {
            "type": "integer",
            "description": "Top edge y coordinate (default: 0 for full screen)",
            "default": 0,
        },
        "width": {
            "type": "integer",
            "description": "Width of capture region (default: full screen width)",
            "default": 0,
        },
        "height": {
            "type": "integer",
            "description": "Height of capture region (default: full screen height)",
            "default": 0,
        },
        "format": {
            "type": "string",
            "description": "Output format: 'base64' (default) or 'path' to save to temp file",
            "default": "base64",
        },
    }

    async def execute(self, **kwargs) -> str:
        err = _check_accessibility()
        if err:
            return err

        x = int(kwargs.get("x", 0))
        y = int(kwargs.get("y", 0))
        width = int(kwargs.get("width", 0))
        height = int(kwargs.get("height", 0))
        fmt = kwargs.get("format", "base64")

        try:
            main_display = Quartz.CGMainDisplayID()
            screen_w = Quartz.CGDisplayPixelsWide(main_display)
            screen_h = Quartz.CGDisplayPixelsHigh(main_display)

            capture_w = width if width > 0 else screen_w
            capture_h = height if height > 0 else screen_h

            rect = Quartz.CGRectMake(x, y, capture_w, capture_h)
            image = Quartz.CGWindowListCreateImage(
                rect,
                kCGWindowListOptionOnScreenOnly,
                kCGNullWindowID,
                kCGWindowImageDefault,
            )

            if image is None:
                return "Error: Failed to capture screen image"

            rep = Quartz.NSBitmapImageRep.alloc().initWithCGImage_(image)
            rep.setSize_(Quartz.NSMakeSize(capture_w, capture_h))
            png_data = rep.representationUsingType_properties_(
                Quartz.NSBitmapImageFileTypePNG, None
            )

            data_bytes = bytes(png_data)
            logger.info("Screen capture: %dx%d, %d bytes", capture_w, capture_h, len(data_bytes))

            if fmt == "path":
                import tempfile
                path = tempfile.mktemp(suffix=".png", prefix="fusion_capture_")
                with open(path, "wb") as f:
                    f.write(data_bytes)
                return path
            else:
                b64 = base64.b64encode(data_bytes).decode("ascii")
                return f"data:image/png;base64,{b64}"

        except Exception as e:
            logger.error("Screen capture failed: %s", e)
            return f"Error: Screen capture failed: {e}"


class MouseTool(BaseTool):
    name = "mouse"
    description = "Control mouse: click, double-click, right-click, move, drag, scroll. Coordinates are in screen pixels (top-left origin)."
    parameters = {
        "action": {
            "type": "string",
            "description": "Mouse action: 'click', 'double_click', 'right_click', 'move', 'drag', 'scroll'",
        },
        "x": {
            "type": "integer",
            "description": "X coordinate on screen (pixels from left)",
        },
        "y": {
            "type": "integer",
            "description": "Y coordinate on screen (pixels from top)",
        },
        "from_x": {
            "type": "integer",
            "description": "Start X for drag action",
            "default": 0,
        },
        "from_y": {
            "type": "integer",
            "description": "Start Y for drag action",
            "default": 0,
        },
        "scroll_delta": {
            "type": "integer",
            "description": "Scroll amount (positive=up, negative=down)",
            "default": 0,
        },
        "button": {
            "type": "string",
            "description": "Mouse button: 'left' (default), 'right', 'middle'",
            "default": "left",
        },
    }

    async def execute(self, **kwargs) -> str:
        err = _check_accessibility()
        if err:
            return err

        action = kwargs.get("action", "")
        x = int(kwargs.get("x", 0))
        y = int(kwargs.get("y", 0))
        button = kwargs.get("button", "left")

        try:
            source = _get_event_source()

            if action == "move":
                event = CGEventCreateMouseEvent(
                    source, kCGEventMouseMoved, (x, y), CGMouseButton.kCGMouseButtonLeft
                )
                CGEventPost(kCGHIDEventTap, event)
                return f"Moved mouse to ({x}, {y})"

            elif action in ("click", "double_click", "right_click", "middle_click"):
                down_type, up_type = self._button_events(button)
                self._click_at(source, x, y, down_type, up_type)
                if action == "double_click":
                    time.sleep(0.05)
                    self._click_at(source, x, y, down_type, up_type)
                label = action.replace("_", "-")
                return f"{label} at ({x}, {y})"

            elif action == "drag":
                from_x = int(kwargs.get("from_x", 0))
                from_y = int(kwargs.get("from_y", 0))
                down_type, up_type = self._button_events(button)
                move_event = CGEventCreateMouseEvent(
                    source, kCGEventMouseMoved, (from_x, from_y), CGMouseButton.kCGMouseButtonLeft
                )
                CGEventPost(kCGHIDEventTap, move_event)
                time.sleep(0.02)
                down_event = CGEventCreateMouseEvent(source, down_type, (from_x, from_y), CGMouseButton.kCGMouseButtonLeft)
                CGEventPost(kCGHIDEventTap, down_event)
                time.sleep(0.05)
                steps = max(1, int(((x - from_x) ** 2 + (y - from_y) ** 2) ** 0.5 / 20))
                for i in range(1, steps + 1):
                    t = i / steps
                    ix = int(from_x + (x - from_x) * t)
                    iy = int(from_y + (y - from_y) * t)
                    drag_event = CGEventCreateMouseEvent(
                        source, kCGEventMouseMoved, (ix, iy), CGMouseButton.kCGMouseButtonLeft
                    )
                    CGEventPost(kCGHIDEventTap, drag_event)
                    time.sleep(0.01)
                up_event = CGEventCreateMouseEvent(source, up_type, (x, y), CGMouseButton.kCGMouseButtonLeft)
                CGEventPost(kCGHIDEventTap, up_event)
                return f"Dragged from ({from_x}, {from_y}) to ({x}, {y})"

            elif action == "scroll":
                scroll_delta = int(kwargs.get("scroll_delta", 3))
                scroll_event = CGEventCreateScrollWheelEvent(
                    source, CGScrollEventUnit.kCGScrollEventUnitPixel, 1, scroll_delta * 10
                )
                CGEventPost(kCGHIDEventTap, scroll_event)
                return f"Scrolled {scroll_delta} units at ({x}, {y})"

            else:
                return f"Error: Unknown mouse action '{action}'. Use: click, double_click, right_click, move, drag, scroll"

        except Exception as e:
            logger.error("Mouse action '%s' failed: %s", action, e)
            return f"Error: Mouse action failed: {e}"

    @staticmethod
    def _button_events(button: str) -> tuple[int, int]:
        if button == "right":
            return kCGEventRightMouseDown, kCGEventRightMouseUp
        elif button == "middle":
            return kCGEventOtherMouseDown, kCGEventOtherMouseUp
        return kCGEventLeftMouseDown, kCGEventLeftMouseUp

    @staticmethod
    def _click_at(source: Any, x: int, y: int, down_type: int, up_type: int) -> None:
        down_event = CGEventCreateMouseEvent(
            source, down_type, (x, y), CGMouseButton.kCGMouseButtonLeft
        )
        CGEventPost(kCGHIDEventTap, down_event)
        time.sleep(0.02)
        up_event = CGEventCreateMouseEvent(
            source, up_type, (x, y), CGMouseButton.kCGMouseButtonLeft
        )
        CGEventPost(kCGHIDEventTap, up_event)


_KEY_MAP: dict[str, int] = {}
if _QUARTZ_OK:
    _KEY_MAP.update({
        "return": 0x24, "enter": 0x24, "tab": 0x30, "space": 0x31,
        "delete": 0x33, "backspace": 0x33, "escape": 0x35, "esc": 0x35,
        "command": 0x37, "cmd": 0x37, "shift": 0x38, "capslock": 0x39,
        "option": 0x3A, "alt": 0x3A, "control": 0x3B, "ctrl": 0x3B,
        "right_shift": 0x3C, "right_option": 0x3D, "right_control": 0x3E,
        "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76, "f5": 0x60,
        "f6": 0x61, "f7": 0x62, "f8": 0x64, "f9": 0x65, "f10": 0x6D,
        "f11": 0x67, "f12": 0x6F,
        "home": 0x73, "end": 0x77, "pageup": 0x74, "pagedown": 0x79,
        "left_arrow": 0x7B, "right_arrow": 0x7C, "down_arrow": 0x7D, "up_arrow": 0x7E,
    })
    for i in range(26):
        _KEY_MAP[chr(ord("a") + i)] = 0x04 + i
    for i in range(10):
        _KEY_MAP[str(i)] = 0x1D + i


class KeyboardTool(BaseTool):
    name = "keyboard"
    description = "Control keyboard: type text, press keys, press keyboard shortcuts."
    parameters = {
        "action": {
            "type": "string",
            "description": "Keyboard action: 'type' (type a string), 'key' (press a single key), 'hotkey' (press key combination)",
        },
        "text": {
            "type": "string",
            "description": "Text to type (for action='type')",
            "default": "",
        },
        "key": {
            "type": "string",
            "description": "Key name to press (for action='key'): e.g. 'return', 'tab', 'escape', 'a'-'z', '0'-'9', 'f1'-'f12', 'cmd', 'shift', 'ctrl', 'alt'",
            "default": "",
        },
        "keys": {
            "type": "string",
            "description": "Comma-separated key names for hotkey (for action='hotkey'): e.g. 'cmd,c' for copy",
            "default": "",
        },
        "delay_ms": {
            "type": "integer",
            "description": "Delay between keystrokes in milliseconds (default: 20)",
            "default": 20,
        },
    }

    async def execute(self, **kwargs) -> str:
        err = _check_accessibility()
        if err:
            return err

        action = kwargs.get("action", "")
        delay_ms = int(kwargs.get("delay_ms", 20)) / 1000.0

        try:
            source = _get_event_source()

            if action == "type":
                text = kwargs.get("text", "")
                if not text:
                    return "Error: 'text' parameter required for type action"
                self._type_string(source, text, delay_ms)
                return f"Typed {len(text)} characters"

            elif action == "key":
                key = kwargs.get("key", "").lower()
                if not key:
                    return "Error: 'key' parameter required for key action"
                if key not in _KEY_MAP:
                    return f"Error: Unknown key '{key}'. Available: {', '.join(sorted(_KEY_MAP.keys()))}"
                keycode = _KEY_MAP[key]
                self._press_key(source, keycode)
                return f"Pressed key: {key}"

            elif action == "hotkey":
                keys_str = kwargs.get("keys", "")
                if not keys_str:
                    return "Error: 'keys' parameter required for hotkey action"
                key_names = [k.strip().lower() for k in keys_str.split(",")]
                keycodes = []
                for k in key_names:
                    if k not in _KEY_MAP:
                        return f"Error: Unknown key '{k}' in hotkey. Available: {', '.join(sorted(_KEY_MAP.keys()))}"
                    keycodes.append(_KEY_MAP[k])
                self._press_hotkey(source, keycodes, key_names)
                return f"Pressed hotkey: {'+'.join(key_names)}"

            else:
                return f"Error: Unknown keyboard action '{action}'. Use: type, key, hotkey"

        except Exception as e:
            logger.error("Keyboard action '%s' failed: %s", action, e)
            return f"Error: Keyboard action failed: {e}"

    @staticmethod
    def _press_key(source: Any, keycode: int) -> None:
        down = CGEventCreateKeyboardEvent(source, keycode, True)
        up = CGEventCreateKeyboardEvent(source, keycode, False)
        CGEventPost(kCGHIDEventTap, down)
        time.sleep(0.02)
        CGEventPost(kCGHIDEventTap, up)

    @staticmethod
    def _press_hotkey(source: Any, keycodes: list[int], names: list[str]) -> None:
        modifier_flags = {
            "cmd": kCGEventFlagMaskCommand,
            "command": kCGEventFlagMaskCommand,
            "shift": kCGEventFlagMaskShift,
            "ctrl": kCGEventFlagMaskControl,
            "control": kCGEventFlagMaskControl,
            "alt": kCGEventFlagMaskShift,
            "option": kCGEventFlagMaskShift,
        }
        flags = 0
        for name in names[:-1]:
            if name in modifier_flags:
                flags |= modifier_flags[name]

        for keycode in keycodes[:-1]:
            down = CGEventCreateKeyboardEvent(source, keycode, True)
            if flags:
                down.setFlags_(flags)
            CGEventPost(kCGHIDEventTap, down)
            time.sleep(0.02)

        last_down = CGEventCreateKeyboardEvent(source, keycodes[-1], True)
        if flags:
            last_down.setFlags_(flags)
        CGEventPost(kCGHIDEventTap, last_down)
        time.sleep(0.02)

        last_up = CGEventCreateKeyboardEvent(source, keycodes[-1], False)
        if flags:
            last_up.setFlags_(flags)
        CGEventPost(kCGHIDEventTap, last_up)

        for keycode in reversed(keycodes[:-1]):
            up = CGEventCreateKeyboardEvent(source, keycode, False)
            CGEventPost(kCGHIDEventTap, up)
            time.sleep(0.01)

    @staticmethod
    def _type_string(source: Any, text: str, delay: float) -> None:
        for ch in text:
            if ch in _KEY_MAP:
                keycode = _KEY_MAP[ch]
                KeyboardTool._press_key(source, keycode)
            else:
                event = CGEventCreateKeyboardEvent(source, 0, True)
                unicodes = [ord(ch)]
                event.setUnicodeString_length_(unicodes, len(unicodes))
                CGEventPost(kCGHIDEventTap, event)
                time.sleep(0.01)
                up_event = CGEventCreateKeyboardEvent(source, 0, False)
                up_event.setUnicodeString_length_(unicodes, len(unicodes))
                CGEventPost(kCGHIDEventTap, up_event)
            time.sleep(delay)


class ClipboardTool(BaseTool):
    name = "clipboard"
    description = "Read from or write to the system clipboard."
    parameters = {
        "action": {
            "type": "string",
            "description": "Clipboard action: 'read' or 'write'",
        },
        "text": {
            "type": "string",
            "description": "Text to write to clipboard (for action='write')",
            "default": "",
        },
    }

    async def execute(self, **kwargs) -> str:
        if not _PLATFORM_OK:
            return "Error: Clipboard tool only works on macOS"

        action = kwargs.get("action", "")

        try:
            import subprocess

            if action == "read":
                result = subprocess.run(
                    ["pbpaste", "-Prefer", "txt"],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    content = result.stdout
                    return content if content else "(clipboard is empty)"
                return f"Error: pbpaste failed with code {result.returncode}"

            elif action == "write":
                text = kwargs.get("text", "")
                result = subprocess.run(
                    ["pbcopy"],
                    input=text, capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    return f"Copied {len(text)} characters to clipboard"
                return f"Error: pbcopy failed with code {result.returncode}"

            else:
                return f"Error: Unknown clipboard action '{action}'. Use: read, write"

        except Exception as e:
            logger.error("Clipboard action '%s' failed: %s", action, e)
            return f"Error: Clipboard action failed: {e}"
