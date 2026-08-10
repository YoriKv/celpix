"""The format picker: a dropdown with category headings and a search field.

The pickers over the plugin registry — pixel, palette, tilemap, compression —
list everything celPix can read, which is now a hundred entries in some of them
and grows with every preset dropped in a plugin folder. A plain
:class:`QComboBox` answers that with one unbroken alphabetical column: to find
the SNES formats you scroll past the Atari ones, and to find a format whose name
you half-remember you scroll twice.

So this adds the two things that list needs and Qt's own popup has nowhere to put
— a **search field** and **category headings** — by replacing the popup rather
than the widget. :class:`SearchableComboBox` is still a ``QComboBox``: the items
live in its own model, ``currentData``/``findData``/``setCurrentIndex`` and the
``activated``/``currentIndexChanged`` signals all behave as before, and
:func:`~celpix.ui.widgets.select_combo_data` and ``signals_blocked`` still work
on it. That is what let every picker move over without its surrounding code
changing, and what keeps a session restore or an undo pushing a selection the
same way it always did.

Where the headings come from is deliberately *not* here: a format states its own
category (:data:`~celpix.plugins.base.CATEGORIES`), so a plugin dropped in a
folder is filed alongside the built-ins with nothing in the UI to teach.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import cast

from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtGui import QKeyEvent, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QLineEdit,
    QListView,
    QVBoxLayout,
    QWidget,
)

from celpix.plugins.base import Plugin, PluginInfo, Preset, category_order
from celpix.ui.widgets import CompactComboBox

# Marks a row as a heading rather than a choosable format. A private sentinel
# object rather than ``None``, because ``None`` is a **value** some pickers store
# (the alphabet combo's "None" entry is a real choice), and a heading that
# answered to ``findData(None)`` would have those selected onto it.
_HEADING = object()

# The popup row's index back into the combo's own model. The two lists differ
# whenever a search is narrowing the popup, so a picked row has to say which
# entry it stands for rather than being trusted as a position.
_SOURCE_ROW = Qt.ItemDataRole.UserRole + 1

# How many rows the popup shows before it scrolls. Sixteen is about a third of a
# 1080p screen: enough that the shorter pickers never scroll at all, short enough
# that the longest one still reads as a dropdown rather than a second window.
MAX_VISIBLE_ROWS = 16

# Below this many entries the popup is Qt's own. A search field over six items
# costs a row of space and a keystroke to skip, and every picker starts small —
# the reshape and alphabet lists are three entries today. The threshold is on the
# *item* count, so a picker grows the search field by itself once a user's plugin
# folder makes it worth having; a picker with headings always gets ours, since
# Qt's popup has nowhere to draw them.
SEARCH_THRESHOLD = 8


def _matches(text: str, needles: Sequence[str]) -> bool:
    """Whether ``text`` contains every word of the search, in any order.

    Word-wise rather than one substring so "snes 4" finds "SNES 4bpp (8x8)":
    these names put the machine, the depth and the tile size in an order the user
    has no reason to have memorised, and a literal-substring search makes the
    user reproduce it.
    """
    lowered = text.lower()
    return all(needle in lowered for needle in needles)


class SearchableComboBox(CompactComboBox):
    """A combo whose popup carries a search field and category headings.

    Fill it with :func:`fill_grouped` (or :meth:`add_category` and the ordinary
    ``addItem``, which is what that does). A heading is an item like any other so
    the model stays one flat list — it is simply not selectable, and every path
    that could land on one skips it: :meth:`add_category` refuses to leave one
    current, Qt's own arrow-key and wheel stepping passes over disabled items,
    and the popup's list cannot select one.

    The popup itself is a top-level ``Qt.Popup``, not Qt's combo view, because
    the search field has to live *inside* the thing that closes when you click
    away. Clicking outside dismisses it, as it would Qt's own.
    """

    def __init__(self, width: int, parent: QWidget | None = None) -> None:
        super().__init__(width, parent)
        self._popup: QFrame | None = None
        self._search: QLineEdit | None = None
        self._list: QListView | None = None
        self._popup_model: QStandardItemModel | None = None
        # Where the popup was anchored, so a rebuild that changes its height
        # grows it away from the combo rather than sliding it over the control.
        self._flipped_above = False
        self._has_headings = False

    # -- filling -----------------------------------------------------------
    def add_category(self, title: str) -> None:
        """Append a heading; the items added after it fall under it.

        The heading is disabled, which is what keeps Qt's own keyboard and wheel
        stepping from stopping on it. It also has to not be *selected*: a combo
        auto-selects the first item inserted into an empty one, so a picker whose
        first row is a heading would open reading its own category name — hence
        the deselection below, undone by the first real item that follows.
        """
        self.addItem(title, _HEADING)
        row = self.count() - 1
        item = self._model_item(row)
        if item is not None:
            font = item.font()
            font.setBold(True)
            item.setFont(font)
            item.setFlags(Qt.ItemFlag.NoItemFlags)
        if self.currentIndex() == row:
            self.setCurrentIndex(-1)
        self._has_headings = True

    def clear(self) -> None:  # Qt override
        super().clear()
        self._has_headings = False

    def is_heading(self, row: int) -> bool:
        """Whether ``row`` is a category heading rather than a choice."""
        return self.itemData(row) is _HEADING

    def first_choice(self) -> int:
        """The first row that is an actual choice, or -1 when there is none.

        The fallback for "select something" — a picker's first row may be a
        heading, so ``setCurrentIndex(0)`` is no longer the safe default it was.
        """
        return next(
            (row for row in range(self.count()) if not self.is_heading(row)), -1
        )

    def _model_item(self, row: int) -> QStandardItem | None:
        model = self.model()
        return model.item(row) if isinstance(model, QStandardItemModel) else None

    # -- the popup ---------------------------------------------------------
    def showPopup(self) -> None:  # Qt override
        """Open the search popup — or Qt's own, for a list too short to search."""
        if self._popup is not None:
            return
        if not self._has_headings and self.count() < SEARCH_THRESHOLD:
            super().showPopup()
            return
        self._build_popup()
        self._rebuild("")
        self._place_popup()
        assert self._popup is not None and self._search is not None
        self._popup.show()
        self._search.setFocus(Qt.FocusReason.PopupFocusReason)

    def hidePopup(self) -> None:  # Qt override
        self._close_popup()
        super().hidePopup()

    def _build_popup(self) -> None:
        popup = QFrame(self, Qt.WindowType.Popup)
        popup.setFrameShape(QFrame.Shape.StyledPanel)
        column = QVBoxLayout(popup)
        column.setContentsMargins(2, 2, 2, 2)
        column.setSpacing(2)

        search = QLineEdit()
        search.setPlaceholderText("Search")
        search.setClearButtonEnabled(True)
        search.textChanged.connect(self._rebuild)
        search.returnPressed.connect(self._activate_current)
        # The arrow keys have to reach the list while the text cursor stays in
        # the field, so they are intercepted rather than routed by focus.
        search.installEventFilter(self)
        column.addWidget(search)

        view = QListView()
        view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        model = QStandardItemModel(view)
        view.setModel(model)
        view.clicked.connect(self._on_row_clicked)
        column.addWidget(view)

        # Qt closes a popup on a click outside without going through hidePopup,
        # so the teardown hangs off the hide itself or the widget leaks and the
        # next open would find a stale one.
        popup.installEventFilter(self)
        self._popup, self._search, self._list, self._popup_model = (
            popup,
            search,
            view,
            model,
        )

    def _close_popup(self) -> None:
        popup, self._popup = self._popup, None
        self._search = self._list = self._popup_model = None
        if popup is not None:
            popup.removeEventFilter(self)
            popup.hide()
            popup.deleteLater()

    def _rebuild(self, text: str) -> None:
        """Refill the popup's list for the current search text.

        A heading is emitted only once one of its items has survived the search,
        which is what stops a narrowed list from being a column of empty
        categories — and why the source list is walked in order rather than
        filtered and regrouped.
        """
        view, model = self._list, self._popup_model
        if view is None or model is None:
            return
        needles = text.lower().split()
        model.clear()
        pending = -1
        for row in range(self.count()):
            if self.is_heading(row):
                pending = row
                continue
            if not _matches(self.itemText(row), needles):
                continue
            if pending >= 0:
                model.appendRow(self._popup_heading(self.itemText(pending)))
                pending = -1
            item = QStandardItem(self.itemText(row))
            item.setData(row, _SOURCE_ROW)
            model.appendRow(item)
        self._select_popup_row(self._popup_row_for(self.currentIndex()))
        self._resize_list()

    @staticmethod
    def _popup_heading(title: str) -> QStandardItem:
        item = QStandardItem(title)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        # Disabled, so it cannot be clicked or arrowed onto, and the style greys
        # it — which is the whole of the visual distinction a heading needs.
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        return item

    def _popup_row_for(self, source_row: int) -> int:
        """Where ``source_row`` sits in the popup now, or the first choice."""
        model = self._popup_model
        if model is None:
            return -1
        first = -1
        for row in range(model.rowCount()):
            data = model.item(row).data(_SOURCE_ROW)
            if data is None:
                continue
            if first < 0:
                first = row
            if data == source_row:
                return row
        return first

    def _select_popup_row(self, row: int) -> None:
        view, model = self._list, self._popup_model
        if view is None or model is None or row < 0 or row >= model.rowCount():
            return
        index = model.index(row, 0)
        view.setCurrentIndex(index)
        view.scrollTo(index, QAbstractItemView.ScrollHint.EnsureVisible)

    def _step_popup_row(self, delta: int) -> None:
        """Move the highlight ``delta`` choices along, stepping over headings."""
        view, model = self._list, self._popup_model
        if view is None or model is None:
            return
        row = view.currentIndex().row()
        row = row if row >= 0 else -1 if delta > 0 else model.rowCount()
        while True:
            row += delta
            if row < 0 or row >= model.rowCount():
                return
            if model.item(row).data(_SOURCE_ROW) is not None:
                self._select_popup_row(row)
                return

    def _resize_list(self) -> None:
        """Fit the popup to what the search left, up to :data:`MAX_VISIBLE_ROWS`.

        Re-measured per keystroke so a narrowed list is a small box rather than
        one mostly empty, and re-anchored the way the popup opened: a popup that
        had to flip above the combo keeps its *bottom* edge on the control, so
        the frame never slides across the picker it belongs to.
        """
        view, model, popup = self._list, self._popup_model, self._popup
        if view is None or model is None or popup is None:
            return
        rows = model.rowCount()
        unit = view.sizeHintForRow(0) if rows else view.fontMetrics().height() + 4
        shown = max(1, min(rows, MAX_VISIBLE_ROWS))
        view.setFixedHeight(shown * unit + 2 * view.frameWidth())
        bottom = popup.geometry().bottom()
        popup.resize(popup.width(), popup.sizeHint().height())
        if self._flipped_above:
            popup.move(popup.x(), bottom - popup.height() + 1)

    def _place_popup(self) -> None:
        """Size the popup to its content and put it under (or over) the combo."""
        popup, view = self._popup, self._list
        if popup is None or view is None:
            return
        # The combo's button is deliberately narrower than its longest item
        # (CompactComboBox), so the popup is widened back to the content — the
        # same trade Qt's own popup gets there, for the same reason.
        width = max(
            self.width(),
            view.sizeHintForColumn(0) + view.verticalScrollBar().sizeHint().width() + 8,
        )
        popup.resize(width, popup.sizeHint().height())
        below = self.mapToGlobal(self.rect().bottomLeft())
        screen = self.screen().availableGeometry()
        x = max(screen.left(), min(below.x(), screen.right() - popup.width() + 1))
        y = below.y()
        self._flipped_above = False
        if y + popup.height() > screen.bottom():
            above = self.mapToGlobal(self.rect().topLeft()).y() - popup.height()
            if above >= screen.top():
                y, self._flipped_above = above, True
            else:
                y = max(screen.top(), screen.bottom() - popup.height() + 1)
        popup.move(QPoint(x, y))

    # -- choosing ----------------------------------------------------------
    def _on_row_clicked(self, index) -> None:
        source = index.data(_SOURCE_ROW)
        if source is not None:
            self._choose(int(source))

    def _activate_current(self) -> None:
        view, model = self._list, self._popup_model
        if view is None or model is None:
            return
        row = view.currentIndex().row()
        if 0 <= row < model.rowCount():
            source = model.item(row).data(_SOURCE_ROW)
            if source is not None:
                self._choose(int(source))

    def _choose(self, source_row: int) -> None:
        """Commit ``source_row`` as the selection, exactly as Qt's popup would.

        ``activated`` fires even when the same entry is re-picked — that is the
        signal's contract, and the tilemap and alphabet pickers are wired to it
        precisely because re-choosing the current format is a meaningful gesture
        there. ``currentIndexChanged`` comes first and only on a real change,
        which ``setCurrentIndex`` takes care of.
        """
        self._close_popup()
        self.setCurrentIndex(source_row)
        self.activated.emit(source_row)
        self.textActivated.emit(self.itemText(source_row))

    # -- events ------------------------------------------------------------
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # Qt override
        if watched is self._popup and event.type() == QEvent.Type.Hide:
            # Clicked away: Qt hid the popup behind our back, so tear it down
            # here or the next open finds a stale one.
            self._close_popup()
            return False
        if watched is self._search and event.type() == QEvent.Type.KeyPress:
            # The type guarantee is the event *type* check above, not the static
            # annotation: Qt hands every filtered event over as a base QEvent.
            return self._on_search_key(cast(QKeyEvent, event))
        return super().eventFilter(watched, event)

    def _on_search_key(self, event: QKeyEvent) -> bool:
        """Steer the list from the search field; True when the key was ours."""
        key = event.key()
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            self._step_popup_row(1 if key == Qt.Key.Key_Down else -1)
            return True
        if key in (Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
            step = MAX_VISIBLE_ROWS if key == Qt.Key.Key_PageDown else -MAX_VISIBLE_ROWS
            for _ in range(abs(step)):
                self._step_popup_row(1 if step > 0 else -1)
            return True
        if key == Qt.Key.Key_Escape:
            self._close_popup()
            return True
        return False

    def _is_real_focus_loss(self, event) -> bool:
        # Our popup takes the keyboard focus for its search field, which is not
        # the user moving on — ``focus_lost`` means the latter, and the pixel
        # picker ends a format-cycling run on it.
        return self._popup is None and super()._is_real_focus_loss(event)


# "no selection asked for", distinct from a selection *of* ``None`` — which the
# alphabet picker really does store, its "None" row being a choice rather than
# the absence of one.
_UNSET = object()


def fill_grouped(
    combo: SearchableComboBox,
    rows: Iterable[tuple[str, str, object]],
    selected: object = _UNSET,
) -> None:
    """Refill ``combo`` from ``(category, label, data)`` rows, in the given order.

    A heading is emitted whenever the category changes, so the caller's sort is
    the whole of the grouping — see :func:`preset_rows` and :func:`plugin_rows`
    for the two sorts every picker here uses. Rows with an empty category simply
    get no heading.

    ``selected`` is snapped to when the refilled list still has it, and otherwise
    falls back to the first real choice — which is what a picker whose stored id
    a plugin refresh has just dropped lands on. It is applied at the end rather
    than per-item because a heading must never be left current, and the first row
    inserted into an empty combo *is* current until something says otherwise.

    Signals are the caller's to block: which repopulations are a user change and
    which are a restore is a question only the caller can answer.
    """
    combo.clear()
    heading = None
    for category, label, data in rows:
        if category and category != heading:
            combo.add_category(category)
        heading = category
        combo.addItem(label, data)
    index = combo.findData(selected) if selected is not _UNSET else -1
    combo.setCurrentIndex(index if index >= 0 else combo.first_choice())


def fill_stage_combo(
    combo: SearchableComboBox, plugins: Iterable[Plugin], selected: str
) -> None:
    """Fill ``combo`` with a stage's plugins, grouped, and select ``selected``.

    Every picker over a stage is the same two steps — list the registered
    plugins, snap to the stored id — and the snap has to survive an id the
    registry no longer has, which is what the fallback to the first real choice
    in :func:`fill_grouped` is for.
    """
    fill_grouped(combo, plugin_rows(plugins), selected)


def preset_rows(
    presets: Iterable[Preset], label: Callable[[Preset], str] | None = None
) -> list[tuple[str, str, str]]:
    """Presets as grouped rows: by category, then by name inside each one.

    ``label`` decorates the text a row shows — the tilemap picker tags each entry
    with the layout its format declares. It is applied **after** the sort, and
    deliberately: sorting the decorated text would re-order a category by its tags
    rather than by name, which is the one thing an alphabetical list must not do.
    """
    ordered = sorted(presets, key=lambda p: (category_order(p.category), p.name))
    return [(p.category, label(p) if label else p.name, p.id) for p in ordered]


def info_rows(infos: Iterable[PluginInfo]) -> list[tuple[str, str, str]]:
    """Plugin descriptors as grouped rows, keeping **registration order** inside
    each category.

    Not name-sorted, unlike :func:`preset_rows`: a stage's plugins are registered
    in a deliberate order (the compression built-ins run none, then the LZ
    family, each scheme beside its improved encoder) and alphabetising would file
    LZ16 between LZ1 and LZ2. Python's sort is stable, so grouping by category
    alone leaves that order intact inside each group.
    """
    return sorted(
        ((info.category, info.name, info.id) for info in infos),
        key=lambda row: category_order(row[0]),
    )


def plugin_rows(plugins: Iterable[Plugin]) -> list[tuple[str, str, str]]:
    """:func:`info_rows` for whole plugins rather than their descriptors."""
    return info_rows(plugin.info for plugin in plugins)
