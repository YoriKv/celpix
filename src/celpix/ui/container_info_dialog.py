"""The container-info popup: what an entry's container made of its file.

A file that opens as something unexpected leaves the user with one question the
rest of the UI cannot answer — *what did the container think this file was?* The
Files row tags the container by name and its tooltip carries any notices, but
neither says which header fields were read, what they were read as, or what the
read passed forward to the stages after it.

So this is a **read-only report**, built by re-running the container stage alone
(:func:`~celpix.pipeline.pipeline.inspect_container`) and laying its
:class:`~celpix.pipeline.pipeline.ContainerReport` out as one name/value table.
Every row carries a tooltip saying what the value was *used for*, which is the
part that cannot be got from a hex editor: the value is in the file, the meaning
is the container's.

Three groups, and the order is the order a reader needs them in — what the
container read (the plugin's own rows), what it published forward (the context
hints, which is how the view came to be the shape it is), and anything it had to
drop or assume on the way (its notices). A container that reports no fields of
its own still gets the summary and the hints, since those come from the host.
"""

from __future__ import annotations

from os.path import basename

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from celpix.core.notices import Notice
from celpix.pipeline.pipeline import ContainerReport
from celpix.plugins.base import ContainerField, format_size

__all__ = ["ContainerInfoDialog"]

# Rows past this many and the table scrolls rather than the dialog growing off
# the screen. A tile bank's report is short; a container with a lot to say (and a
# third-party one may) must not push the OK button past the bottom edge.
_VISIBLE_ROWS = 16


def _size(count: int) -> str:
    """A byte count in both forms — ``"8 KiB (8192 bytes)"`` — where they differ.

    The round form is what a size is quoted in and the exact one is what a reader
    checks an offset against, so a payload gets both. A count that is not a whole
    multiple already *is* its exact form, and saying it twice reads as a bug.
    """
    pretty = format_size(count)
    return pretty if pretty.endswith("bytes") else f"{pretty} ({count} bytes)"


def _summary_fields(report: ContainerReport) -> tuple[ContainerField, ...]:
    """The rows the **host** knows, whatever the container had to say.

    The extent question — how much of the file became the payload — is answered
    here rather than left to each plugin, since it is the same question for all
    of them and the host has both numbers. It is also the one row that is worth
    reading on a container that frames nothing at all.
    """
    fields = [
        ContainerField(
            "Container",
            report.container_name,
            "The plugin this entry's bytes are read through.\n"
            "Change it with Edit File Container… if the file is\n"
            "not what detection took it for.",
        ),
    ]
    if len(report.paths) > 1:
        fields.append(
            ContainerField(
                "Files",
                f"{len(report.paths)} joined end to end",
                "The container is handed the joined buffer and never\n"
                "learns there was more than one file. Every offset\n"
                "below addresses that join, not any one file.",
            )
        )
    fields.append(
        ContainerField(
            "Source",
            _size(report.source_size),
            "What the container was handed: the whole file, never a\n"
            "pre-cut window, since where the payload begins is the\n"
            "container's own answer.",
        )
    )
    fields.append(
        ContainerField(
            "Payload",
            _size(report.payload_size),
            "What came back out and went on to be decoded. Less than\n"
            "the source wherever framing was stripped; the difference\n"
            "is what a save has to put back.",
        )
    )
    return tuple(fields)


def _notice_fields(notices: tuple[Notice, ...]) -> tuple[ContainerField, ...]:
    """The read's notices as rows — the summary named by its level.

    A notice is already a summary and a detail, which is this table's shape
    exactly; the level goes in the name column because "warning" is the part that
    decides whether the row needs acting on.
    """
    return tuple(
        ContainerField(notice.level.value.capitalize(), notice.summary, notice.detail)
        for notice in notices
    )


class ContainerInfoDialog(QDialog):
    def __init__(self, report: ContainerReport, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        name = basename(report.paths[0]) if report.paths else report.container_name
        self.setWindowTitle(f"Container Info - {name}")

        layout = QVBoxLayout(self)
        heading = QLabel(name)
        font = heading.font()
        font.setBold(True)
        heading.setFont(font)
        heading.setToolTip("\n".join(report.paths))
        layout.addWidget(heading)

        if report.error:
            # Shown rather than raised: the read failing is itself the answer the
            # user came for, and whatever the container published before it gave
            # up is usually what explains the failure — so the table still runs.
            failure = QLabel(f"The container's read failed: {report.error}")
            failure.setWordWrap(True)
            failure.setStyleSheet("color: #a08040;")
            layout.addWidget(failure)

        self._table = QTableWidget(0, 2)
        self._table.setHorizontalHeaderLabels(["Field", "Value"])
        self._table.verticalHeader().hide()
        # A report, not a form: nothing here is editable, and the row-at-a-time
        # selection is only so a value can be read off with the keyboard.
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setWordWrap(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table)

        self._fill(report)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        # Room for a value and its field name without eliding either, measured in
        # characters so it follows the font rather than a pixel guess.
        self.setMinimumWidth(self.fontMetrics().averageCharWidth() * 64)

    # -- building ------------------------------------------------------------
    def _fill(self, report: ContainerReport) -> None:
        """Lay the report's three groups out, skipping any that is empty.

        A section header is a row spanning both columns rather than a separate
        widget per group, so the field and value columns stay aligned down the
        whole table — the point of the layout is that every value lines up.
        """
        groups = (
            ("Read by the container", (*_summary_fields(report), *report.fields)),
            ("Passed to later stages", report.hints),
            ("Notices", _notice_fields(report.notices)),
        )
        for title, fields in groups:
            if not fields:
                continue
            self._add_section(title)
            for field in fields:
                self._add_row(field)
        self._table.resizeRowsToContents()
        rows = min(self._table.rowCount(), _VISIBLE_ROWS)
        # Measured off a real row so it holds at any font size or DPI. The
        # header is outside the rows' own height and has to be added back.
        unit = self._table.rowHeight(0) if self._table.rowCount() else 0
        self._table.setMinimumHeight(
            rows * unit + self._table.horizontalHeader().height() + 2
        )

    def _add_section(self, title: str) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        item = QTableWidgetItem(title)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)  # a heading is not selectable
        self._table.setItem(row, 0, item)
        self._table.setSpan(row, 0, 1, 2)

    def _add_row(self, field: ContainerField) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        for column, text in ((0, field.name), (1, field.value)):
            item = QTableWidgetItem(text)
            # Both cells carry the tooltip: which one the pointer lands on is an
            # accident of where the value happens to end.
            item.setToolTip(field.detail or field.value)
            self._table.setItem(row, column, item)

    # -- entry point ---------------------------------------------------------
    @staticmethod
    def show_report(parent: QWidget | None, report: ContainerReport) -> None:
        """Run the popup modally over ``parent``."""
        ContainerInfoDialog(report, parent).exec()
