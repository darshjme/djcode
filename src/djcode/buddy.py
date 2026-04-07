"""DJcode Buddy — persistent ASCII companion that lives beside your prompt.

Inspired by Claude Code's companion system, but uniquely DarshJ-themed.
The buddy reacts to what's happening: thinking, success, error, idle.

Species: diya (oil lamp), cobra, lotus, chai, peacock, om
Each user gets a deterministic buddy based on their username hash.
"""

from __future__ import annotations

import hashlib
import os
import random
from dataclasses import dataclass
from typing import Any

from rich.text import Text


# ── Species sprites (5 lines tall, ~12 wide) ────────────────────────────

SPRITES: dict[str, dict[str, list[str]]] = {
    "diya": {
        "idle": [
            "    ,    ",
            "   /|\\   ",
            "  ( * )  ",
            "  |___|  ",
            " /_____|\\",
        ],
        "thinking": [
            "   ,*,   ",
            "  //|\\\\  ",
            "  ( ~ )  ",
            "  |___|  ",
            " /_____|\\",
        ],
        "success": [
            "  .*+*,  ",
            "  /|*|\\  ",
            "  ( ^ )  ",
            "  |___|  ",
            " /_____|\\",
        ],
        "error": [
            "    .    ",
            "   /|\\   ",
            "  ( x )  ",
            "  |___|  ",
            " /_____|\\",
        ],
    },
    "cobra": {
        "idle": [
            "   _/\\_  ",
            "  / .. \\ ",
            "  \\ ~~ / ",
            "   |  |  ",
            "   \\__/  ",
        ],
        "thinking": [
            "   _/\\_  ",
            "  / °° \\ ",
            "  \\ -- / ",
            "   |..|  ",
            "   \\__/  ",
        ],
        "success": [
            "   _/\\_  ",
            "  / ^^ \\ ",
            "  \\ \\/ / ",
            "   |  |  ",
            "   \\__/  ",
        ],
        "error": [
            "   _/\\_  ",
            "  / xx \\ ",
            "  \\ >< / ",
            "   |  |  ",
            "   \\__/  ",
        ],
    },
    "lotus": {
        "idle": [
            "  .-\"-. ",
            " /  |  \\",
            "(  -o-  )",
            " \\ | / ",
            "  ~~~~~  ",
        ],
        "thinking": [
            "  .-\"-. ",
            " / .|. \\",
            "(  -~-  )",
            " \\ | / ",
            "  ~~~~~  ",
        ],
        "success": [
            "  .***. ",
            " / *|* \\",
            "(  -^-  )",
            " \\ | / ",
            "  ~~~~~  ",
        ],
        "error": [
            "  .---. ",
            " / .|. \\",
            "(  -x-  )",
            " \\ | / ",
            "  ~~~~~  ",
        ],
    },
    "chai": {
        "idle": [
            "  ~~~~~~ ",
            "  |    | ",
            "  | \u2615 | ",
            "  |    | ",
            "  \\____/ ",
        ],
        "thinking": [
            " ~~\u00b0~~~ ",
            "  |    | ",
            "  | \u2615 | ",
            "  |    | ",
            "  \\____/ ",
        ],
        "success": [
            " ~*~*~* ",
            "  |    | ",
            "  | \u2615 | ",
            "  |  \u2713 | ",
            "  \\____/ ",
        ],
        "error": [
            "  ~~~~~~ ",
            "  |    | ",
            "  | \u2615 | ",
            "  |  \u2717 | ",
            "  \\____/ ",
        ],
    },
    "peacock": {
        "idle": [
            " \\|/|\\/ ",
            "  (\u00b0\u00b0)  ",
            "  /||\\  ",
            " / || \\ ",
            "  _/\\_  ",
        ],
        "thinking": [
            " \\|*|\\/ ",
            "  (~~)  ",
            "  /||\\  ",
            " / || \\ ",
            "  _/\\_  ",
        ],
        "success": [
            " *|*|*/ ",
            "  (^^)  ",
            "  /||\\  ",
            " / || \\ ",
            "  _/\\_  ",
        ],
        "error": [
            " \\|.|\\/ ",
            "  (xx)  ",
            "  /||\\  ",
            " / || \\ ",
            "  _/\\_  ",
        ],
    },
    "om": {
        "idle": [
            "   \u0950    ",
            "  / \\   ",
            " | . |  ",
            "  \\ /   ",
            "   ~    ",
        ],
        "thinking": [
            "  *\u0950*   ",
            "  / \\   ",
            " | ~ |  ",
            "  \\ /   ",
            "   ~    ",
        ],
        "success": [
            "  +\u0950+   ",
            "  / \\   ",
            " | ^ |  ",
            "  \\ /   ",
            "   ~    ",
        ],
        "error": [
            "   \u0950    ",
            "  / \\   ",
            " | x |  ",
            "  \\ /   ",
            "   ~    ",
        ],
    },
}

SPECIES_LIST = list(SPRITES.keys())
NAMES = [
    "Agni", "Vayu", "Surya", "Chandra", "Prithvi",
    "Dhara", "Jyoti", "Kavi", "Mitra", "Nila",
    "Ravi", "Tara", "Veda", "Arjun", "Shakti",
    "Bodhi", "Rishi", "Meera", "Lakshya", "Daksh",
]

TITLES = [
    "the Illuminated", "the Swift", "the Serene",
    "the Watchful", "the Steadfast", "the Wise",
]

# ── Species emoji mapping ────────────────────────────────────────────────

SPECIES_EMOJI: dict[str, str] = {
    "diya": "\U0001fa94",   # oil lamp
    "cobra": "\U0001f40d",  # snake
    "lotus": "\U0001fab7",  # lotus
    "chai": "\u2615",       # hot beverage
    "peacock": "\U0001f99a",  # peacock
    "om": "\U0001f549\ufe0f",  # om
}

MOOD_EMOJI: dict[str, str] = {
    "idle": "",        # use species emoji
    "thinking": "\u23f3",  # hourglass
    "success": "\u2728",   # sparkles
    "error": "\u274c",     # cross mark
}


@dataclass
class Buddy:
    """A persistent ASCII companion for the DJcode CLI."""

    name: str
    species: str
    title: str
    mood: str = "idle"  # idle, thinking, success, error

    @classmethod
    def from_username(cls, username: str | None = None) -> Buddy:
        """Generate a deterministic buddy from the username."""
        user = username or os.environ.get("USER", os.environ.get("USERNAME", "darsh"))
        seed = int(hashlib.md5(user.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)

        species = rng.choice(SPECIES_LIST)
        name = rng.choice(NAMES)
        title = rng.choice(TITLES)

        return cls(name=name, species=species, title=title)

    @property
    def emoji(self) -> str:
        """Get the current emoji based on mood. Uses species emoji when idle."""
        if self.mood == "idle":
            return SPECIES_EMOJI.get(self.species, "\u2615")
        return MOOD_EMOJI.get(self.mood, SPECIES_EMOJI.get(self.species, "\u2615"))

    def get_sprite(self, mood: str | None = None) -> list[str]:
        """Get the current sprite lines for the buddy."""
        m = mood or self.mood
        species_sprites = SPRITES.get(self.species, SPRITES["diya"])
        return species_sprites.get(m, species_sprites["idle"])

    def render(self, mood: str | None = None) -> str:
        """Render the buddy as a string block."""
        lines = self.get_sprite(mood)
        return "\n".join(lines)

    def render_rich(self, mood: str | None = None) -> Text:
        """Render as Rich Text with gold color."""
        sprite = self.render(mood)
        text = Text(sprite)
        text.stylize("bold #FFD700")
        return text

    def greeting(self) -> str:
        """Generate a buddy greeting message."""
        greetings = {
            "diya": f"\U0001fa94 {self.name} {self.title} lights your path.",
            "cobra": f"\U0001f40d {self.name} {self.title} guards your code.",
            "lotus": f"\U0001fab7 {self.name} {self.title} blooms beside you.",
            "chai": f"\u2615 {self.name} {self.title} brews fresh ideas.",
            "peacock": f"\U0001f99a {self.name} {self.title} displays with pride.",
            "om": f"\U0001f549\ufe0f {self.name} {self.title} resonates with clarity.",
        }
        return greetings.get(self.species, f"{self.name} {self.title} is here.")

    def react(self, event: str) -> str:
        """Get a buddy reaction message for an event."""
        reactions = {
            "thinking": {
                "diya": "flame flickers as it contemplates...",
                "cobra": "sways gently, sensing the answer...",
                "lotus": "petals curl inward, focusing...",
                "chai": "steam swirls with thought...",
                "peacock": "feathers rustle with concentration...",
                "om": "hums quietly, processing...",
            },
            "success": {
                "diya": "flame burns bright!",
                "cobra": "hood spreads wide with pride!",
                "lotus": "blooms fully!",
                "chai": "overflows with warmth!",
                "peacock": "fans out in celebration!",
                "om": "resonates with harmony!",
            },
            "error": {
                "diya": "flame dims... but still burns.",
                "cobra": "recoils, then steadies.",
                "lotus": "wilts slightly, then recovers.",
                "chai": "goes cold... reheating.",
                "peacock": "ruffles feathers, undeterred.",
                "om": "wavers, then finds center.",
            },
        }
        event_reactions = reactions.get(event, {})
        msg = event_reactions.get(self.species, "reacts.")
        return f"{self.name} {msg}"

    def set_mood(self, mood: str) -> None:
        """Update the buddy's mood."""
        if mood in ("idle", "thinking", "success", "error"):
            self.mood = mood


# ── Singleton ────────────────────────────────────────────────────────────

_buddy: Buddy | None = None


def get_buddy() -> Buddy:
    """Get or create the singleton buddy instance."""
    global _buddy
    if _buddy is None:
        _buddy = Buddy.from_username()
    return _buddy
