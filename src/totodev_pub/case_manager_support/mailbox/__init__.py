# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""The file-drop mailbox: request and result types, and the transport itself.

Only the writer half lives here. The half that *executes* requests against a
running fleet is ``case_manager_support.signaling_adapter`` — a split that lets a
submitting client import this package without dragging in the scheduling layer.
"""

from totodev_pub.case_manager_support.mailbox.transport import (
    MAILBOX_PROTOCOL_VERSION,
    AdoptRequest,
    FireRequest,
    MailboxResult,
    MailboxTransport,
    ReclassifyRequest,
    ReclassifyResult,
    RequestHandle,
    arrival_order,
)

__all__ = [
    "MAILBOX_PROTOCOL_VERSION",
    "AdoptRequest",
    "FireRequest",
    "MailboxResult",
    "MailboxTransport",
    "ReclassifyRequest",
    "ReclassifyResult",
    "RequestHandle",
    "arrival_order",
]
