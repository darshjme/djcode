"""Original, offline UI design guidance bundled with DJcode (MIT)."""
from importlib.resources import files

_PACKS = (
    ("dashboard", "Operational dashboard", "Prioritize decisions, current data and drill-down paths."),
    ("settings", "Settings and preferences", "Make scope, saved state and risky changes explicit."),
    ("command-palette", "Command palette", "Search and execute commands with predictable keyboard behavior."),
    ("onboarding-auth", "Onboarding and authentication", "Connect an account with reversible steps and honest capability checks."),
    ("data-table", "Data table", "Compare records with accessible sorting, filtering and selection."),
    ("usage-billing", "Usage and billing", "Explain consumption, periods, estimates and paid changes clearly."),
    ("empty-error", "Empty and error states", "Distinguish absence, filtering, permissions and recoverable failures."),
)


def list_packs() -> list[dict[str, str]]:
    """Return independent metadata dictionaries in a stable reading order."""
    return [{"id": key, "title": title, "summary": summary} for key, title, summary in _PACKS]


def get_pack(pack_id: str) -> str:
    """Read an allowlisted package resource; never interpret input as a path."""
    allowed = {key for key, _, _ in _PACKS}
    if not isinstance(pack_id, str) or pack_id not in allowed:
        raise ValueError("Unknown design pack. Choose: " + ", ".join(key for key, _, _ in _PACKS))
    return files("djcode").joinpath("design_patterns", pack_id + ".md").read_text(encoding="utf-8")


def get_example(pack_id: str) -> str:
    """Return an original SVG illustration from the same strict pack allowlist."""
    if not isinstance(pack_id, str) or pack_id not in {key for key, _, _ in _PACKS}:
        raise ValueError("Unknown design pack. Choose: " + ", ".join(key for key, _, _ in _PACKS))
    return files("djcode").joinpath("design_patterns", pack_id + ".svg").read_text(encoding="utf-8")


def get_license() -> str:
    """Return the project MIT notice for self-contained reference exports."""
    return files("djcode").joinpath("design_patterns", "LICENSE").read_text(encoding="utf-8")
