"""Entry point for the Tower of Fantasy window capturer.

Run with the project virtualenv:

    .venv\\Scripts\\python.exe main.py
"""

from __future__ import annotations

import os
import sys

# Make the ``src`` layout importable before importing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from tof_automation.ui import HOTKEY_LABEL, MainWindow  # noqa: E402


def selftest() -> int:
    """Smoke-test a packaged build: runtime paths, icon templates, WinRT OCR.

    A frozen GUI app has no console, and a missing hidden import (winsdk's lazily
    imported WinRT namespaces are the usual suspect) would otherwise only surface
    mid-run as a silent failure. Run ``ToF-Auto-CA.exe --selftest`` after
    building: it exercises the same imports the loop needs, writes the result to
    ``selftest.txt`` beside the exe, and exits non-zero if anything is broken.
    """
    from pathlib import Path

    report: list[str] = []
    ok = True

    def check(label: str, fn) -> None:
        nonlocal ok
        try:
            report.append(f"[ok]   {label}: {fn()}")
        except Exception as exc:  # noqa: BLE001 - the report is the output
            ok = False
            report.append(f"[FAIL] {label}: {exc!r}")

    report.append(f"frozen={getattr(sys, 'frozen', False)}  exe={sys.executable}")

    from tof_automation import config, detector, icons, ocr

    check("config path", lambda: f"{config.config_path()} "
          f"(exists={config.config_path().exists()})")
    check("templates dir", lambda: f"{icons.templates_dir()} "
          f"(exists={icons.templates_dir().exists()})")
    check("icon templates", lambda: f"{len(icons.IconLibrary().load().templates)} loaded")
    check("opencv", lambda: __import__("cv2").__version__)
    check("numpy", lambda: __import__("numpy").__version__)
    check("mss", lambda: __import__("mss").__name__)
    check("pydirectinput", lambda: __import__("pydirectinput").__name__)
    check("win32gui", lambda: __import__("win32gui").__name__)

    def ocr_roundtrip() -> str:
        """Render a known string, OCR it back, and parse it like the loop would."""
        from PIL import Image, ImageDraw, ImageFont

        text = "Matching cooldown ends in 23s."
        img = Image.new("RGB", (460, 60), (12, 14, 20))
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 22)
        except OSError:
            font = ImageFont.load_default()
        ImageDraw.Draw(img).text((10, 15), text, font=font, fill=(240, 240, 240))
        b, g, r = img.split()[2], img.split()[1], img.split()[0]
        bgra = Image.merge("RGBA", (b, g, r, Image.new("L", img.size, 255))).tobytes()
        result = ocr.recognize_bgra(bgra, img.width, img.height)
        read = " ".join(line.text for line in result.lines)
        seconds = detector.parse_matching_cooldown(read)
        if seconds != 23:
            raise RuntimeError(f"OCR read {read!r} -> parsed {seconds}, expected 23")
        return f"read {read!r} -> {seconds}s"

    check("WinRT OCR", ocr_roundtrip)

    report.append("RESULT: " + ("all checks passed" if ok else "FAILURES ABOVE"))
    text = chr(10).join(report)
    print(text)
    try:
        out = Path(sys.executable).parent / "selftest.txt"
        out.write_text(text + chr(10), encoding="utf-8")
        print(f"(written to {out})")
    except OSError:
        pass
    return 0 if ok else 1


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()

    app = QApplication(sys.argv)

    window = MainWindow()
    window.install_hotkeys(app)
    window.show()

    if not window.register_hotkeys():
        QMessageBox.critical(
            window,
            "Hotkey unavailable",
            f"Could not register the global hotkeys ({HOTKEY_LABEL} / F8). One may "
            "already be in use by another program. Close the conflicting app and "
            "restart.",
        )
        return 1

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
