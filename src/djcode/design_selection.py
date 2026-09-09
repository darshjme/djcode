"""Select one bundled design reference while preserving the conversation protocol."""
from djcode.design_packs import get_pack, list_packs

_START = "\n\n[DJCODE_DESIGN_REFERENCE]\n"
_END = "\n[/DJCODE_DESIGN_REFERENCE]"


def select_pack(operator, identifier: str) -> str:
    content = None if identifier == "off" else get_pack(identifier)
    if not operator or not operator.messages or operator.messages[0].role != "system":
        raise ValueError("Start a conversation before selecting a design reference.")
    system = operator.messages[0]
    before, marker, after = system.content.partition(_START)
    if marker:
        _, closing, suffix = after.partition(_END)
        if not closing:
            raise ValueError("Existing design reference is malformed; conversation retained.")
        base = before + suffix
    else:
        base = system.content
    system.content = base if content is None else (
        base + _START + "Optional design reference. Apply it only where relevant to the user's task; "
        "preserve the user's requirements and the operational rules above.\n\n" + content + _END
    )
    if content is None:
        return "Design reference cleared."
    title = next(pack["title"] for pack in list_packs() if pack["id"] == identifier)
    return f"Design reference selected: {title}. Describe what you want to build; /design off clears it."
