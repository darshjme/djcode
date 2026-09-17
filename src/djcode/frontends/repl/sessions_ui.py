"""Finding a past session, and getting back into it.

`/resume` accepted one thing: a full uuid4, exactly. The way to obtain one was
to run `/history`, read a table, and retype thirty-six characters. There was no
`--continue`, no `--resume`, no picker, and no way to ask the only question
anyone actually asks -- "put me back in what I was doing in this folder".

Three ways in now, all of them over the same resolver:

* ``djcode --continue`` / ``-c`` -- the most recent session in this directory,
  no argument at all.
* ``djcode --resume [id]`` -- that session, or a picker when no id is given.
* ``/resume`` inside the REPL -- an id, a unique PREFIX of one, or nothing,
  which opens the picker.

A prefix that matches two sessions resolves to neither. Resuming the wrong
conversation because two uuids happened to share four characters is a silent
loss of the one the user meant, and "be more specific" is a much better
outcome than "here is somebody else's work".
"""

from __future__ import annotations

from typing import Any

from djcode.sessions import Session, SessionDB

__all__ = [
    "describe",
    "pick_session",
    "restore_into",
    "resolve",
]


def describe(session: Session, *, width: int = 60) -> str:
    """One line: when, how big, and what it was about."""
    when = (session.last_interaction_at or session.updated_at or session.start or "")[:16]
    when = when.replace("T", " ")
    summary = (session.summary or "").strip().replace("\n", " ")
    if not summary:
        summary = "(no summary)"
    if len(summary) > width:
        summary = summary[: width - 1] + "…"
    return (
        f"{session.id[:8]}  {when}  {session.messages_count:>4} msg  "
        f"{session.model:<18.18} {summary}"
    )


def resolve(reference: str, db: SessionDB | None = None) -> tuple[str | None, str]:
    """``(session_id, reason)``. ``session_id`` is None when nothing matched."""
    db = db or SessionDB()
    reference = (reference or "").strip()
    if not reference:
        return None, "No session id given."
    if db.get_session(reference) is not None:
        return reference, ""
    resolved = db.resolve_session_id(reference)
    if resolved:
        return resolved, ""
    # Distinguish "no such session" from "be more specific": the fixes differ.
    matches = [s for s in db.list_sessions(limit=500) if s.id.startswith(reference)]
    if len(matches) > 1:
        return None, f"{len(matches)} sessions start with {reference!r}. Use more characters."
    return None, f"No session matches {reference!r}."


async def pick_session(
    db: SessionDB | None = None, cwd: str | None = None, limit: int = 15
) -> str | None:
    """Offer recent sessions and return the chosen id, or None if cancelled.

    This-directory sessions come first and are labelled, because that is what
    the user is nearly always after; everything else follows so a session
    started in the wrong folder is still reachable.
    """
    import os

    import questionary

    from djcode.frontends.repl.theme import questionary_style

    db = db or SessionDB()
    cwd = cwd or os.getcwd()
    here = db.sessions_for_cwd(cwd, limit=limit)
    here_ids = {s.id for s in here}
    elsewhere = [s for s in db.list_sessions(limit=limit * 3) if s.id not in here_ids][:limit]
    if not here and not elsewhere:
        return None

    choices: list[Any] = []
    if here:
        choices.append(questionary.Separator("-- this folder --"))
        choices.extend(questionary.Choice(describe(s), value=s.id) for s in here)
    if elsewhere:
        choices.append(questionary.Separator("-- elsewhere --"))
        choices.extend(questionary.Choice(describe(s), value=s.id) for s in elsewhere)

    return await questionary.select(
        "Resume which session?", choices=choices, style=questionary_style()
    ).ask_async()


def restore_into(operator: Any, session_id: str, db: SessionDB | None = None) -> int:
    """Replay a stored conversation into a live operator. Returns the count.

    The CURRENT system prompt is kept and the stored one dropped. A system
    prompt carries the tool schemas and the model's name; replaying last
    week's would describe tools this build may no longer have.
    """
    from djcode.provider import Message

    db = db or SessionDB()
    stored = db.load_conversation(session_id)
    if not stored:
        return 0

    system = operator.messages[0] if operator.messages else None
    operator.messages.clear()
    if system is not None:
        operator.messages.append(system)

    restored = 0
    for message in stored:
        role = message.get("role", "")
        if role == "system":
            continue
        operator.messages.append(
            Message(
                role=role,
                content=message.get("content", ""),
                tool_calls=message.get("tool_calls") or [],
                tool_call_id=message.get("tool_call_id"),
                name=message.get("name"),
                images=message.get("images", []),
            )
        )
        restored += 1

    operator.session_id = session_id
    manager = getattr(operator, "context_manager", None)
    if manager is not None:
        try:
            # Without this the context meter keeps reporting the EMPTY session
            # that was just replaced, so a resumed 80%-full conversation shows
            # as 0% until the next turn.
            manager.replace_messages(operator.messages)
        except Exception:  # pragma: no cover - the meter must not fail a resume
            pass
    return restored
