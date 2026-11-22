"""Which camera a name belongs to today, once cameras have been renamed.

A clip keeps the name its camera had when it was recorded, so that the
library, its filters and the security timeline still say what they said at
the time. Everything a camera *learns* or is *configured with* (its
description, prompt, baselines, feedback) is kept under its current name, so
anything that starts from a clip's own name has to ask which camera that name
is now. This is that one answer; stdlib only, so the database, the analyzer
and the app can all use it without importing each other.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping


def current_camera_name(
    name: str, aliases: Mapping[str, str], live: Iterable[str] = ()
) -> str:
    """The camera *name* has become, following renames recorded in *aliases*.

    *aliases* maps an old name to the name it was renamed to (see
    ``camera_name_aliases.json``), matched without regard to capitalisation
    like every other camera lookup. A name that a camera on the account
    carries today is that camera whatever an old alias says -- a name can be
    taken again after its camera was renamed away -- so the chain stops at
    the first live name. *live* is only trusted when it is not empty: an empty
    list is a startup window before Blink has reported its cameras, not "no
    cameras", and every alias then applies. A name with no alias is returned as it was
    given, and a chain that loops ends where it first repeats (a rename that
    only changed capitals ends on the new spelling).
    """
    lookup = {str(old).lower(): str(new) for old, new in aliases.items() if old and new}
    live_names = {str(n).lower() for n in live}
    current = name
    seen: set[str] = set()
    while True:
        key = current.lower()
        if key in live_names or key in seen:
            return current
        seen.add(key)
        target = lookup.get(key)
        if target is None:
            return current
        current = target
