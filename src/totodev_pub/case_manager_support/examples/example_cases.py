# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Two small case types the examples share, so each script stays about hosting.

Designing case types is a separate subject with its own tooling (the
``case-designer`` skill). These exist only to give the manager something real to
drive: one that advances on its own, and one that waits to be told.
"""

from __future__ import annotations

import asyncio

from totodev_pub.folder_backed_case import FolderBackedCase


# Both types declare an empty ``asset_aliases``: they have no protocol-elevated
# data objects to register. The library warns about that on purpose — for a real
# case type an empty alias set usually means someone forgot — but for an example
# whose subject is hosting rather than data modelling, it is the honest answer.
class InquiryCase(FolderBackedCase):
    """Advances on its own — the manager's sweep walks it to done unaided.

    ``--`` edges are automatic: the pool driver takes them whenever a case is
    due, which is what makes a fleet of these self-completing.
    """

    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["[*] --> received -- triage --> reviewed -- resolve --> closed --> [*]"]

    async def perform_triage(self, tctx) -> None:
        await asyncio.sleep(0.05)   # stand-in for real work

    async def perform_resolve(self, tctx) -> None:
        await asyncio.sleep(0.05)


class EscalationCase(FolderBackedCase):
    """Waits to be told. ``==`` edges are manual: only a ``fire()`` moves it.

    A fleet of these sits idle until something outside asks for a step — which
    is what the fire mailbox is for.
    """

    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["[*] --> waiting == approve ==> approved --> [*]"]
