"""Root's side of the outbox (SPEC-D.md section 1, SPEC-E.md section 4): HTML cards and documents for the bot.

An item is `{"html": [message, ...], "reply_to"?}` (messages already escaped and split by agentic.cards) or
`{"document": "<name>.doc", "filename", "caption", "reply_to"?}`. A document file is written before its item and
removed again when the item cannot be written, so the bot never sees an item without its file.
"""

import os

from agentic import cards, spool
from agentic.config import Config


def _owner(cfg: Config) -> tuple[int, int] | None:
    return spool.root_owner(cfg.bot_user)


def write_cards(cfg: Config, blocks: list[str], name: str | None = None, reply_to: int | None = None) -> str:
    """One outbox item holding the card's messages. Errors of spool.write_item propagate."""
    payload: dict = {"html": cards.pack(blocks)}
    if reply_to is not None:
        payload["reply_to"] = reply_to
    return spool.write_item(cfg.outbox, payload, name=name, owner=_owner(cfg))


def write_document(cfg: Config, data: bytes, filename: str, caption: str, name: str | None = None,
                   reply_to: int | None = None) -> str:
    """The document file, then its item. spool.DocumentTooLarge (nothing written) over DOCUMENT_MAX_BYTES.
    `caption` is HTML made by agentic.cards."""
    document = spool.write_document(cfg.outbox, data, name=name, owner=_owner(cfg))
    payload: dict = {"document": document, "filename": filename, "caption": caption}
    if reply_to is not None:
        payload["reply_to"] = reply_to
    try:
        return spool.write_item(cfg.outbox, payload, name=name, owner=_owner(cfg))
    except BaseException:
        dfd = spool.open_dir(cfg.outbox)
        try:
            spool.remove(dfd, document)
        finally:
            os.close(dfd)
        raise
