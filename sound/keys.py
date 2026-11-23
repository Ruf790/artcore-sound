"""The keyboard shortcuts as people read them (1.0): «Ctrl+↑», «Пробел» — for the tool-bar tips, the Hotkeys window
and the README's table, all made from the window's actions, so they can never disagree with what the keys do."""
from __future__ import annotations

from .i18n import t

ARROWS = {"Up": "↑", "Down": "↓", "Left": "←", "Right": "→"}


def key_text(seq) -> str:
    """A QKeySequence (or its portable text) → «Ctrl+Shift+↑», «Пробел», «Del»."""
    from PySide6.QtGui import QKeySequence
    s = seq if isinstance(seq, str) else seq.toString(QKeySequence.PortableText)
    parts = s.split("+")
    out = []
    for i, p in enumerate(parts):
        if p == "" and i == len(parts) - 1 and out:        # «Ctrl++»
            out.append("+")
            continue
        if p == "Space":
            p = t("key_space")
        elif p == "Delete":
            p = "Del"
        elif p == "Escape":
            p = "Esc"
        out.append(ARROWS.get(p, p))
    return "+".join(x for x in out if x)


def action_keys(a) -> str:
    """All the shortcuts of an action, «Ctrl+Y / Ctrl+Shift+Z»."""
    return " / ".join(key_text(k) for k in a.shortcuts())


def plain(text: str) -> str:
    """The menu text without «…» and «&»."""
    return text.replace("&", "").rstrip("…").strip()


def tip(a, text: str = "") -> str:
    """A tool-bar tip from an action: its text and its first shortcut — «Отменить: Обрезать (Ctrl+Z)»."""
    k = key_text(a.shortcuts()[0]) if a.shortcuts() else ""
    base = text or plain(a.text())
    return f"{base} ({k})" if k else base


def table(menubar) -> list[tuple[str, str, str]]:
    """(menu, command, keys) of every action with a shortcut, in the order of the menus."""
    rows = []

    def walk(menu, top, sub):
        for a in menu.actions():
            if a.isSeparator():
                continue
            if a.menu() is not None:
                walk(a.menu(), top, sub + [plain(a.text())])
                continue
            if a.shortcuts():
                name = a.property("acName") or plain(a.text())      # Undo / Redo: not «Undo: Cut»
                rows.append((top, " → ".join(sub + [name]), action_keys(a)))
    for a in menubar.actions():
        if a.menu() is not None:
            walk(a.menu(), plain(a.text()), [])
    return rows


MOUSE = ("ks_m_select", "ks_m_extend", "ks_m_all", "ks_m_wheel", "ks_m_zoom", "ks_m_marker", "ks_m_rename",
         "ks_m_overview", "ks_m_drop")
