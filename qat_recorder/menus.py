# -*- coding: utf-8 -*-
"""
Menus — the one control Qat addresses by label.

A menu item in a widgets application is a `QAction`. It is not a widget, has no
geometry of its own and never receives the click; the menu paints it and keeps
it in a list. Everything the rest of this project relies on therefore fails on
menus: there is no object to name, and clicking the object that *did* receive
the event -- the menu, or the menu bar -- lands in the middle of a bar that is
as wide as the window.

Qat solves this in its own way, and this module simply speaks Qat's language.
Qat wraps each item in a virtual widget with the item's geometry, found by the
menu that contains it plus the item's text::

    menu_bar  = {"type": "QMenuBar"}
    file_item = {"container": menu_bar, "text": "File"}
    qat.mouse_click(file_item)                  # opens the File menu

    file_menu = {"objectName": "fileMenu"}
    open_item = {"container": file_menu, "text": "Open"}
    qat.mouse_click(open_item)                  # activates Open

Two consequences worth stating plainly:

* **The label is the address.** There is no objectName-based route to a menu
  item, so a menu step is as translation-sensitive as the menu is. That is a
  property of the toolkit, not a shortcut taken here, and the generated script
  says so.
* **The menu must already be open.** Its items do not exist as far as a lookup
  is concerned until it is, which is why a menu interaction always replays as
  two clicks: one to open, one to choose.

QML needs none of this. There a menu item is a real object that receives the
click itself, so the ordinary resolver handles it.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from qat_recorder.ir import Robustness, Target

#: Classes whose items are painted rather than parented.
MENU_CLASSES = frozenset({"QMenu", "QMenuBar"})

MENU_SUFFIXES = ("Menu", "MenuBar")

LABEL_WARNING = ("menu items are addressed by their label; this step will break "
                 "under translation")

NOT_FOUND = "menu item could not be identified while its menu was open"


def is_menu(class_name: str) -> bool:
    """Whether this class draws its items instead of parenting them."""
    name = (class_name or "").strip()
    return name in MENU_CLASSES or name.endswith(MENU_SUFFIXES)


def strip_mnemonic(text: str) -> str:
    """Remove the `&` Qt uses to mark a keyboard mnemonic.

    "&Options" is what the property holds; "Options" is what the user reads and
    what Qat matches. A doubled `&&` is a literal ampersand and survives.
    """
    if "&" not in text:
        return text
    return "\x00".join(text.split("&&")).replace("&", "").replace("\x00", "&")


def candidate_labels(text: str) -> list:
    """Label spellings to try, most likely first."""
    stripped = strip_mnemonic(text).strip()
    labels = [stripped]
    # Qat's documentation says the ampersand is optional, which implies it
    # normalises. If a version does not, the raw text still matches.
    if text.strip() and text.strip() not in labels:
        labels.append(text.strip())
    # Menu items routinely end in an ellipsis to signal "opens a dialog", and
    # the two spellings of it are easy to confuse.
    for label in list(labels):
        for suffix in ("...", "…"):
            if label.endswith(suffix):
                trimmed = label[: -len(suffix)].strip()
                if trimmed and trimmed not in labels:
                    labels.append(trimmed)
    return labels


def resolve_menu_item(backend, container: Mapping[str, Any], text: str,
                      item_name: str = "") -> Optional[Target]:
    """A validated Target for one item of an open menu, or None.

    `container` is the definition of the menu or menu bar that was clicked, as
    the ordinary resolver named it.

    Validation is weaker here than everywhere else in this project: elsewhere a
    candidate is accepted only when it finds *the very object* being named, and
    a menu item has no object to compare against. Exactly one match is all that
    can be checked -- so it is checked, live, while the menu is open, which
    still catches the ambiguous and the misspelt before the recording is saved.
    """
    if not text or not container:
        return None

    for label in candidate_labels(text):
        definition = {"container": dict(container), "text": label}
        try:
            matches = backend.find_all(definition)
        except Exception:                                    # noqa: BLE001
            continue
        if len(matches) != 1:
            continue
        return Target(
            definition=definition,
            strategy="menu item",
            # Scoped to a named menu and validated live. Not STRONG: the label
            # is user-visible text, and nothing else in this project treats
            # user-visible text as strong.
            robustness=Robustness.MODERATE,
            warnings=(LABEL_WARNING,),
            label=strip_mnemonic(text).strip() or (item_name or "menu item"),
        )
    return None
