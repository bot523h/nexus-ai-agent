"""Shared fixtures for the AETHEL director tests.

Imported as a sibling (``import director_fixtures``), never as ``tests.*``.
"""

from __future__ import annotations

SIMPLE_SCRIPT = """Title: The Key
Author: A. Writer

INT. LIGHTHOUSE - NIGHT

Mara stands at the window. She takes the brass key from the table.

MARA
I will keep it safe.

[[a note about the key]]

# ACT ONE

= Mara decides to hide the key

INT. HARBOUR - DAWN

Ivo waits. Mara hands the brass key to Ivo.

IVO
You came back.

CUT TO:
"""

MULTI_SCENE_SCRIPT = """INT. ROOM - DAY

Alice waits.

ALICE
Hello.

EXT. STREET - NIGHT

Bob arrives.

BOB
You are late.

> FADE OUT.

INT. ROOM - DAWN

Alice returns.
"""


def three_actor_script() -> str:
    return """INT. OFFICE - DAY

Alice works. Bob enters. Carol follows.

ALICE
Good morning.

BOB
Morning.

CAROL
Hello everyone.
"""
