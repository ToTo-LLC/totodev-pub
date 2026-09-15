# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""The file-drop request channel: the envelope, its payloads, and the transport.

Only the writer half lives here. The half that *executes* requests against a
running fleet is ``case_manager_support.signaling_adapter`` — a split that lets a
submitting client import this package without dragging in the scheduling layer.
"""

from totodev_pub.case_manager_support.mailbox.transport import (
    ADOPT_OP,
    FIRE_OP,
    MAILBOX_PROTOCOL_VERSION,
    RECLASSIFY_OP,
    AdoptPayload,
    FirePayload,
    MailboxResult,
    MailboxTransport,
    ReclassifyPayload,
    ReclassifyResult,
    RequestEnvelope,
    RequestHandle,
    arrival_order,
    case_key_for,
)

__all__ = [
    "ADOPT_OP",
    "FIRE_OP",
    "MAILBOX_PROTOCOL_VERSION",
    "RECLASSIFY_OP",
    "AdoptPayload",
    "FirePayload",
    "MailboxResult",
    "MailboxTransport",
    "ReclassifyPayload",
    "ReclassifyResult",
    "RequestEnvelope",
    "RequestHandle",
    "arrival_order",
    "case_key_for",
]
