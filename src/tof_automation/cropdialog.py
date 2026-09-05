"""Drag-to-crop dialog for capturing an icon template from a frame.

Shows a captured window frame; the user drags a rectangle around the icon and
names it. Returns the selection rectangle in *original image* coordinates so the
caller can crop the full-resolution template.
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QRect, QSize, Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QLabel,
    QRadioButton,
    QRubberBand,
    QVBoxLayout,
)

from .icons import KNOWN_ICONS

_MAX_DISPLAY = QSize(1100, 680)


class _CropLabel(QLabel):
    """A pixmap label that lets the user rubber-band a selection rectangle."""

    def __init__(self, pixmap: QPixmap) -> None:
        super().__init__()
        self.setPixmap(pixmap)
        self.setFixedSize(pixmap.size())
        self._origin = QPoint()
        self._band = QRubberBand(QRubberBand.Shape.Rectangle, self)
        self.selection = QRect()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._origin = event.pos()
        self._band.setGeometry(QRect(self._origin, QSize()))
        self._band.show()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if not self._origin.isNull():
            self._band.setGeometry(QRect(self._origin, event.pos()).normalized())

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.selection = QRect(self._origin, event.pos()).normalized()


class CropDialog(QDialog):
    """Modal dialog returning a named crop rectangle in original-image coords."""

    def __init__(self, pixmap: QPixmap, suggested_name: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Capture icon — drag a box around it, then name it")

        self._original_size = pixmap.size()
        display = pixmap.scaled(
            _MAX_DISPLAY,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._scale = (
            self._original_size.width() / display.width()
            if display.width()
            else 1.0
        )
        self._label = _CropLabel(display)

        # Name is chosen from the known icons via radio buttons (exact names, no
        # typos). The button group holds one radio per KNOWN_ICONS entry.
        self._name_group = QButtonGroup(self)
        name_box = QGroupBox("Which icon is this?")
        name_layout = QVBoxLayout(name_box)
        for name, hint in KNOWN_ICONS:
            radio = QRadioButton(f"{name} — {hint}")
            radio.setProperty("icon_name", name)
            if name == suggested_name:
                radio.setChecked(True)
            self._name_group.addButton(radio)
            name_layout.addWidget(radio)

        # Some buttons have visual variants (e.g. priority_entry: red-dot / "new"
        # tag / plain). Tick this to keep the existing capture and add another.
        self._variant = QCheckBox(
            "Save as an additional variant (keep existing capture of this icon)"
        )

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Drag a box around the icon:"))
        layout.addWidget(self._label)
        layout.addWidget(name_box)
        layout.addWidget(self._variant)
        layout.addWidget(buttons)

    @property
    def icon_name(self) -> str:
        checked = self._name_group.checkedButton()
        return checked.property("icon_name") if checked else ""

    @property
    def as_variant(self) -> bool:
        """Whether to keep any existing capture and add this as a new variant."""
        return self._variant.isChecked()

    def selection_in_image(self) -> QRect | None:
        """Selection mapped to original-image pixel coordinates, or None."""
        sel = self._label.selection
        if sel.width() < 4 or sel.height() < 4:
            return None
        s = self._scale
        return QRect(
            int(sel.x() * s), int(sel.y() * s),
            int(sel.width() * s), int(sel.height() * s),
        )
