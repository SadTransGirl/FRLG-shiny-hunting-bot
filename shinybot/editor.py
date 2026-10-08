"""Window for editing a button sequence: notes, timings, order."""

from __future__ import annotations

import copy
import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, simpledialog, ttk

import customtkinter as ctk

from . import theme
from .recorder import merge_repeats
from .sequence import Step, sequence_duration

COLUMNS = ('press', 'hold', 'wait', 'random_wait', 'repeat', 'note')
HEADINGS = {'press': 'Buttons', 'hold': 'Hold (s)', 'wait': 'Wait after (s)',
            'random_wait': 'Random + (s)', 'repeat': 'Repeat', 'note': 'Note'}
WIDTHS = {'press': 200, 'hold': 100, 'wait': 150, 'random_wait': 140, 'repeat': 90, 'note': 330}


def parse_cell(column: str, text: str):
    """Validate an edited cell; raises ValueError with a readable message."""
    text = text.strip()
    if column == 'press':
        press = text.upper().replace(' ', '')
        Step(press).buttons  # raises ValueError for unknown buttons
        return press
    if column in ('hold', 'wait', 'random_wait'):
        value = float(text)
        if value < 0:
            raise ValueError('time must be 0 or more')
        return round(value, 2)
    if column == 'repeat':
        value = int(text)
        if value < 1:
            raise ValueError('repeat must be 1 or more')
        return value
    return text


def _button(parent, text: str, command, primary: bool = False) -> ctk.CTkButton:
    return ctk.CTkButton(parent, text=text, command=command, height=34, width=10, corner_radius=10,
                         font=theme.F['small'], border_width=0,
                         fg_color=theme.ACCENT if primary else theme.PANEL_ALT,
                         hover_color=theme.ACCENT_HOVER if primary else theme.PANEL_HOVER,
                         text_color=theme.ACCENT_TEXT if primary else theme.TEXT)


class SequenceEditor(ctk.CTkToplevel):
    def __init__(self, master, name: str, steps: list[Step],
                 on_save: Callable[[str, list[Step]], None]) -> None:
        super().__init__(master, fg_color=theme.BG)
        self.name = name
        self.steps = copy.deepcopy(steps)
        self.on_save = on_save
        self.dirty = False
        self._editor: tk.Entry | None = None
        self._editing: tuple[int, str] = (0, 'press')
        self._showing_error = False
        self.protocol('WM_DELETE_WINDOW', self.close)
        self._build()
        self._refresh()
        self._update_title()
        # CustomTkinter sets its own icon on new windows after a moment; replace it after that.
        self.after(250, lambda: theme.set_icon(self))
        self.after(100, self.lift)

    def _build(self) -> None:
        ctk.CTkLabel(self, text=(
            'Double-click a cell to edit it (Tab = next cell, Enter = done, Esc = cancel). '
            '"Wait after" is the pause after a press, before the next one. "Random +" adds\n'
            'a random extra pause (0 to that many seconds) so every reset gets a different '
            'starter: put it on the title screen and in the lab before taking the Poke Ball.'),
            font=theme.F['small'], text_color=theme.TEXT_MUTED, anchor='w', justify='left').pack(
            fill='x', padx=14, pady=(10, 6))
        card = ctk.CTkFrame(self, fg_color=theme.PANEL, corner_radius=theme.RADIUS)
        card.pack(fill='both', expand=True, padx=12)
        frame = tk.Frame(card, bg=theme.PANEL)
        frame.pack(fill='both', expand=True, padx=10, pady=10)
        self.tree = ttk.Treeview(frame, columns=('n',) + COLUMNS, show='headings', height=12,
                                 selectmode='browse')
        self.tree.heading('n', text='#')
        self.tree.column('n', width=50, anchor='e', stretch=False)
        for column in COLUMNS:
            self.tree.heading(column, text=HEADINGS[column])
            self.tree.column(column, width=WIDTHS[column],
                             anchor='w' if column in ('press', 'note') else 'e',
                             stretch=column == 'note')
        scroll = ctk.CTkScrollbar(frame, command=self.tree.yview, fg_color=theme.PANEL,
                                  button_color=theme.PANEL_ALT, button_hover_color=theme.PANEL_HOVER)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        self.tree.bind('<Double-1>', self._on_double_click)
        self.tree.bind('<Delete>', lambda _e: self.delete())

        buttons = ctk.CTkFrame(self, fg_color='transparent')
        buttons.pack(fill='x', padx=12, pady=10)
        for text, command in [('Up', lambda: self.move(-1)), ('Down', lambda: self.move(1)),
                              ('Insert', self.insert), ('Duplicate', self.duplicate),
                              ('Delete', self.delete), ('Merge repeats', self.merge)]:
            _button(buttons, text, command).pack(side='left', padx=(0, 6))
        _button(buttons, 'Close', self.close).pack(side='right')
        _button(buttons, 'Save as', self.save, primary=True).pack(side='right', padx=6)
        self.summary = ctk.CTkLabel(buttons, text='', font=theme.F['small'],
                                    text_color=theme.TEXT_MUTED)
        self.summary.pack(side='right', padx=12)

    # -- display --------------------------------------------------------------

    def _update_title(self) -> None:
        self.title(f'Sequence: {self.name}{" *" if self.dirty else ""}')

    def _refresh(self, select: int | None = None) -> None:
        self.tree.delete(*self.tree.get_children())
        for index, step in enumerate(self.steps):
            self.tree.insert('', 'end', iid=str(index), values=(
                index + 1, step.press, f'{step.hold:g}', f'{step.wait:g}',
                f'{step.random_wait:g}' if step.random_wait else '', step.repeat, step.note))
        if select is not None and self.steps:
            iid = str(max(0, min(select, len(self.steps) - 1)))
            self.tree.selection_set(iid)
            self.tree.see(iid)
        total = sum(s.repeat for s in self.steps)
        self.summary.configure(
            text=f'{len(self.steps)} steps, {total} presses, ~{sequence_duration(self.steps):.0f}s')

    def _changed(self, select: int | None = None) -> None:
        self.dirty = True
        self._update_title()
        self._refresh(select)

    def _selected(self) -> int | None:
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    # -- row actions ------------------------------------------------------------

    def move(self, offset: int) -> None:
        index = self._selected()
        if index is None or not 0 <= index + offset < len(self.steps):
            return
        self.steps[index], self.steps[index + offset] = self.steps[index + offset], self.steps[index]
        self._changed(index + offset)

    def insert(self) -> None:
        index = self._selected()
        index = len(self.steps) if index is None else index + 1
        self.steps.insert(index, Step('A', hold=0.1, wait=1.0))
        self._changed(index)

    def duplicate(self) -> None:
        index = self._selected()
        if index is not None:
            self.steps.insert(index + 1, copy.deepcopy(self.steps[index]))
            self._changed(index + 1)

    def delete(self) -> None:
        index = self._selected()
        if index is not None:
            del self.steps[index]
            self._changed(index)

    def merge(self) -> None:
        merged = merge_repeats(self.steps)
        if len(merged) == len(self.steps):
            messagebox.showinfo('Merge repeats', 'Nothing to merge.', parent=self)
            return
        self.steps = merged
        self._changed(0)

    # -- cell editing -----------------------------------------------------------

    def _on_double_click(self, event) -> None:
        row = self.tree.identify_row(event.y)
        column_id = self.tree.identify_column(event.x)  # '#1' is the step number
        if not row or column_id in ('', '#1'):
            return
        self.edit_cell(int(row), COLUMNS[int(column_id[1:]) - 2])

    def edit_cell(self, index: int, column: str) -> None:
        self._close_editor(commit=True)
        iid = str(index)
        self.tree.see(iid)
        self.tree.selection_set(iid)
        self.update_idletasks()
        bbox = self.tree.bbox(iid, column)
        if not bbox:
            return
        x, y, width, height = bbox
        entry = tk.Entry(self.tree, bg=theme.PANEL_ALT, fg=theme.TEXT, insertbackground=theme.ACCENT,
                         relief='flat', highlightthickness=1, highlightcolor=theme.ACCENT,
                         highlightbackground=theme.BORDER, font=(theme.FAMILY['pixel'], -24))
        entry.insert(0, str(getattr(self.steps[index], column)))
        entry.select_range(0, 'end')
        entry.place(x=x, y=y, width=max(width, 80), height=height)
        entry.focus_set()
        entry.bind('<Return>', lambda _e: self._close_editor(commit=True))
        entry.bind('<KP_Enter>', lambda _e: self._close_editor(commit=True))
        entry.bind('<Escape>', lambda _e: self._close_editor(commit=False))
        entry.bind('<Tab>', lambda _e: self._next_cell(index, column, 1))
        entry.bind('<Shift-Tab>', lambda _e: self._next_cell(index, column, -1))
        entry.bind('<ISO_Left_Tab>', lambda _e: self._next_cell(index, column, -1))
        entry.bind('<FocusOut>', lambda _e: self._showing_error or self._close_editor(commit=True))
        self._editor = entry
        self._editing = (index, column)

    def _next_cell(self, index: int, column: str, direction: int) -> str:
        if not self._close_editor(commit=True):
            return 'break'
        position = index * len(COLUMNS) + COLUMNS.index(column) + direction
        if 0 <= position < len(self.steps) * len(COLUMNS):
            self.edit_cell(position // len(COLUMNS), COLUMNS[position % len(COLUMNS)])
        return 'break'

    def _close_editor(self, commit: bool) -> bool:
        """Returns False if the value was invalid (the editor stays open)."""
        entry, self._editor = self._editor, None
        if entry is None:
            return True
        index, column = self._editing
        if commit:
            try:
                value = parse_cell(column, entry.get())
            except ValueError as error:
                self._editor = entry
                self.bell()
                self._showing_error = True
                try:
                    messagebox.showerror('Invalid value', f'{HEADINGS[column]}: {error}', parent=self)
                finally:
                    self._showing_error = False
                entry.focus_set()
                return False
            if getattr(self.steps[index], column) != value:
                setattr(self.steps[index], column, value)
                self.dirty = True
                self._update_title()
        entry.destroy()
        self._refresh(index)
        return True

    # -- save / close -----------------------------------------------------------

    def save(self) -> bool:
        if not self._close_editor(commit=True):
            return False
        name = simpledialog.askstring('Save sequence', 'Name for this sequence:',
                                      initialvalue=self.name, parent=self)
        if not name or not name.strip():
            return False
        self.name = name.strip()
        self.on_save(self.name, copy.deepcopy(self.steps))
        self.dirty = False
        self._update_title()
        return True

    def close(self) -> None:
        self._close_editor(commit=True)
        if self.dirty:
            answer = messagebox.askyesnocancel('Unsaved changes', 'Save this sequence first?',
                                               parent=self)
            if answer is None or (answer and not self.save()):
                return
        self.destroy()
