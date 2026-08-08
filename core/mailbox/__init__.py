# core/mailbox/ — Mailbox Provider Module
from core.mailbox.base import BaseMailboxProvider, MailboxAccount
from core.mailbox.forwarded_domain import ForwardedDomainMailbox

def get_cfworker():
    """Lazy import CFWorkerMailbox to avoid circular dependency."""
    from core.mailbox_providers import CFWorkerMailbox
    return CFWorkerMailbox

__all__ = ["BaseMailboxProvider", "MailboxAccount", "ForwardedDomainMailbox", "get_cfworker"]
