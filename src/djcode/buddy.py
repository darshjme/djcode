"""DJcode Buddy — Fenwick-style ASCII companion with speech bubbles.

A persistent dharmic-themed ASCII creature that lives in your terminal.
Reacts to events with contextual speech bubbles. Has idle fidget animations.

Species: diya (oil lamp), cobra, lotus, chai, peacock, om
Each user gets a deterministic buddy based on their username hash.
"""

from __future__ import annotations

import hashlib
import os
import re
import random
import shutil
import textwrap
import time
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.text import Text


# ── Constants ──────────────────────────────────────────────────────────────

GOLD = "#FFD700"
SPRITE_WIDTH = 14          # max char width of any sprite
BUBBLE_MAX_WIDTH = 32      # inner text width of speech bubble
BUBBLE_SHOW_SEC = 8.0      # how long bubble stays visible
MIN_COLS_FOR_SPRITE = 60   # collapse below this terminal width
MIN_COLS_FOR_BUBBLE = 80   # no bubble below this width

# Idle animation: indices into frames. -1 = blink
IDLE_SEQUENCE = [0, 0, 0, 0, 1, 0, 0, 0, -1, 0, 0, 2, 0, 0, 0]


# ── Sprites (3 frames each, 5 lines tall) ─────────────────────────────────
# Frame 0 = rest, Frame 1 = fidget A, Frame 2 = fidget B
# {E} placeholder replaced with eye character

BODIES: dict[str, list[list[str]]] = {
    "diya": [
        [  # frame 0: rest
            "      ,      ",
            "     /|\\     ",
            "    ({E}*{E})    ",
            "    |___|    ",
            "   /_____\\   ",
        ],
        [  # frame 1: flame flicker
            "     ,*,     ",
            "    //|\\\\    ",
            "    ({E}~{E})    ",
            "    |___|    ",
            "   /_____\\   ",
        ],
        [  # frame 2: big flame
            "    .*+*.    ",
            "    /|*|\\    ",
            "    ({E}^{E})    ",
            "    |___|    ",
            "   /_____\\   ",
        ],
    ],
    "cobra": [
        [  # frame 0: rest
            "     _/\\_    ",
            "    / {E}{E} \\   ",
            "    \\ ~~ /   ",
            "     |  |    ",
            "     \\__/    ",
        ],
        [  # frame 1: sway left
            "    _/\\_     ",
            "   / {E}{E} \\    ",
            "   \\ ~~ /    ",
            "    |..|     ",
            "    \\__/     ",
        ],
        [  # frame 2: sway right
            "      _/\\_   ",
            "     / {E}{E} \\  ",
            "     \\ ~~ /  ",
            "      |..|   ",
            "      \\__/   ",
        ],
    ],
    "lotus": [
        [  # frame 0: rest
            "    .-\"-.    ",
            "   / {E}|{E} \\   ",
            "  (  -o-  )  ",
            "   \\ | /     ",
            "    ~~~~~    ",
        ],
        [  # frame 1: petals curl
            "    .'\"\".    ",
            "   /{E} | {E}\\   ",
            "  (  ~o~  )  ",
            "   \\ | /     ",
            "    ~~~~~    ",
        ],
        [  # frame 2: bloom
            "   .*\"\".*.   ",
            "   /*{E}|{E}*\\   ",
            "  (  ^o^  )  ",
            "   \\ | /     ",
            "    ~~~~~    ",
        ],
    ],
    "peacock": [
        [  # frame 0: rest
            "   \\|/|\\|/   ",
            "    ({E}{E})     ",
            "    /||\\     ",
            "   / || \\    ",
            "    _/\\_     ",
        ],
        [  # frame 1: fan out
            "  *\\|*|\\|/*  ",
            "    ({E}{E})     ",
            "    /||\\     ",
            "   / || \\    ",
            "    _/\\_     ",
        ],
        [  # frame 2: strut
            "   \\|/|\\|/   ",
            "     ({E}{E})    ",
            "     /||\\    ",
            "    / || \\   ",
            "     _/\\_    ",
        ],
    ],
    "om": [
        [  # frame 0: rest
            "     \u0950      ",
            "    / \\     ",
            "   |{E}.{E}|    ",
            "    \\ /     ",
            "     ~      ",
        ],
        [  # frame 1: pulse
            "    *\u0950*     ",
            "    / \\     ",
            "   |{E}~{E}|    ",
            "    \\ /     ",
            "     ~      ",
        ],
        [  # frame 2: glow
            "   +*\u0950*+    ",
            "    / \\     ",
            "   |{E}^{E}|    ",
            "    \\ /     ",
            "     ~      ",
        ],
    ],
}

EYES = [".", "\u00b7", "\u2022", "\u00b0", "*", "o"]

SPECIES_LIST = list(BODIES.keys())

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

SPECIES_EMOJI: dict[str, str] = {
    "diya": "\U0001fa94",
    "cobra": "\U0001f40d",
    "lotus": "\U0001fab7",
    "peacock": "\U0001f99a",
    "om": "\U0001f549\ufe0f",
}


# ── Contextual commentary ─────────────────────────────────────────────────
# Quips the buddy says in speech bubbles, keyed by event type

QUIPS: dict[str, dict[str, list[str]]] = {
    "thinking": {
        "diya": [
            "flame flickers, contemplating...",
            "oil burns steady — thinking deep.",
            "the wick draws an answer...",
        ],
        "cobra": [
            "hood sways, sensing patterns...",
            "scales shimmer with thought...",
            "coiled tight, processing...",
        ],
        "lotus": [
            "petals curl inward, focusing...",
            "roots draw from deep memory...",
            "pond ripples with thought...",
        ],
        "peacock": [
            "feathers rustle, concentrating...",
            "tail folds to focus...",
            "strutting through the logic...",
        ],
        "om": [
            "hums quietly, processing...",
            "vibrations align...",
            "seeking resonance...",
        ],
    },
    "success": {
        "diya": [
            "flame burns bright!",
            "illuminated! The answer shines.",
            "the lamp guides true.",
        ],
        "cobra": [
            "hood spreads wide with pride!",
            "venom of knowledge strikes!",
            "the cobra delivers.",
        ],
        "lotus": [
            "blooms fully!",
            "petals open with clarity.",
            "beauty in the answer.",
        ],
        "peacock": [
            "fans out in celebration!",
            "colors shine with pride!",
            "magnificent display!",
        ],
        "om": [
            "resonates with harmony!",
            "the universe aligns.",
            "cosmic clarity achieved.",
        ],
    },
    "error": {
        "diya": [
            "flame dims... but still burns.",
            "the wind blows, but I endure.",
            "oil low, refueling...",
        ],
        "cobra": [
            "recoils, then steadies.",
            "missed the strike. Again.",
            "venom replenished. Retry.",
        ],
        "lotus": [
            "wilts slightly, then recovers.",
            "muddy waters, but roots hold.",
            "a petal fell. Growing another.",
        ],
        "peacock": [
            "ruffles feathers, undeterred.",
            "a feather fell. Still fabulous.",
            "brief stumble. Still strutting.",
        ],
        "om": [
            "wavers, then finds center.",
            "dissonance... retuning.",
            "the frequency shifts. Adapting.",
        ],
    },
    "commit": {
        "diya": ["sealed in flame. Committed."],
        "cobra": ["the code is marked. No escape."],
        "lotus": ["planted in the record."],
        "peacock": ["displayed for all to see."],
        "om": ["etched in the cosmic ledger."],
    },
    "tool_use": {
        "diya": ["lighting the way for tools..."],
        "cobra": ["striking at the filesystem..."],
        "lotus": ["reaching into the codebase..."],
        "peacock": ["fanning out the operation..."],
        "om": ["channeling through tools..."],
    },
    "greeting": {
        "diya": [
            "lights your path. Let's code.",
            "the flame awaits your command.",
        ],
        "cobra": [
            "guards your code. Ready.",
            "coiled and ready to strike.",
        ],
        "lotus": [
            "blooms beside you. Begin.",
            "the pond is calm. Let's flow.",
        ],
        "peacock": [
            "displays with pride. Let's ship.",
            "feathers at the ready.",
        ],
        "om": [
            "resonates. Channel your intent.",
            "the frequency is set. Speak.",
        ],
    },
    "idle": {
        "diya": ["the flame sways gently..."],
        "cobra": ["tongue flicks, waiting..."],
        "lotus": ["floating peacefully..."],
        "peacock": ["preening quietly..."],
        "om": ["hums a low note..."],
    },
}


# ── Smart context observer ─────────────────────────────────────────────────
# Analyzes actual conversation content to generate contextual commentary
# instead of random generic quips. This is what makes the buddy "smart".

# Patterns to detect in responses
_CODE_BLOCK_RE = re.compile(r"```(\w*)\n", re.MULTILINE)
_FILE_PATH_RE = re.compile(r"(?:^|\s)((?:[~/]|[\w]+/)[\w./\-]+\.\w+)", re.MULTILINE)
_ERROR_KEYWORDS = re.compile(
    r"\b(error|exception|traceback|failed|crash|panic|segfault|ENOENT|denied|refused"
    r"|timeout|404|500|undefined|null|NoneType)\b",
    re.IGNORECASE,
)
_FIX_KEYWORDS = re.compile(
    r"\b(fix|fixed|resolve|resolved|patch|patched|repair|corrected|solved)\b",
    re.IGNORECASE,
)
_EXPLAIN_KEYWORDS = re.compile(
    r"\b(because|means|essentially|basically|in other words|the reason)\b",
    re.IGNORECASE,
)
_TEST_KEYWORDS = re.compile(
    r"\b(test|tests|testing|pytest|unittest|spec|assert|expect|passed|failed)\b",
    re.IGNORECASE,
)
_REFACTOR_KEYWORDS = re.compile(
    r"\b(refactor|rename|extract|simplify|clean|reorganize|restructure|move)\b",
    re.IGNORECASE,
)


@dataclass
class ConversationContext:
    """Tracks conversation state for smart buddy reactions."""

    turn_count: int = 0
    consecutive_successes: int = 0
    consecutive_errors: int = 0
    last_user_query: str = ""
    last_response_length: int = 0
    tools_used: list[str] = field(default_factory=list)
    files_mentioned: list[str] = field(default_factory=list)
    languages_seen: list[str] = field(default_factory=list)

    def update_from_exchange(
        self,
        user_input: str,
        response: str,
        tools: list[str] | None = None,
        success: bool = True,
    ) -> None:
        """Update context from a user/assistant exchange."""
        self.turn_count += 1
        self.last_user_query = user_input
        self.last_response_length = len(response)

        if success:
            self.consecutive_successes += 1
            self.consecutive_errors = 0
        else:
            self.consecutive_errors += 1
            self.consecutive_successes = 0

        if tools:
            self.tools_used = tools

        # Extract file paths mentioned
        files = _FILE_PATH_RE.findall(response)
        if files:
            self.files_mentioned = files[-3:]  # last 3

        # Extract languages from code blocks
        langs = _CODE_BLOCK_RE.findall(response)
        if langs:
            self.languages_seen = [l for l in langs if l][-3:]


def generate_smart_quip(
    species: str,
    event: str,
    ctx: ConversationContext,
    response: str = "",
    error_msg: str = "",
    rng: random.Random | None = None,
) -> str:
    """Generate a context-aware quip based on what actually happened.

    Analyzes the response content, user query, and conversation state
    to produce relevant commentary — not just random flavor text.
    """
    rng = rng or random.Random()

    # ── Error events: comment on the actual error ──────────────────────
    if event == "error" and error_msg:
        if "connection" in error_msg.lower() or "timeout" in error_msg.lower():
            return rng.choice([
                f"connection dropped. The wire went cold.",
                f"timed out — the server ghosted us.",
                f"lost the signal. Retry?",
            ])
        if "model" in error_msg.lower() or "ollama" in error_msg.lower():
            return rng.choice([
                f"model hiccup. Is Ollama running?",
                f"can't reach the model. Check /models.",
                f"model went silent. Try /model to switch.",
            ])
        if ctx.consecutive_errors >= 3:
            return rng.choice([
                f"third strike. Maybe rethink the approach?",
                f"{ctx.consecutive_errors} errors in a row. Time to step back.",
                f"something deeper is off. Let's debug.",
            ])
        # Generic error with actual message excerpt
        short = error_msg[:40].rstrip()
        return f'hit: "{short}..."'

    # ── Success events: analyze what the response contains ─────────────
    if event == "success" and response:
        # Detect what kind of response this was
        has_code = bool(_CODE_BLOCK_RE.search(response))
        has_fix = bool(_FIX_KEYWORDS.search(response))
        has_explanation = bool(_EXPLAIN_KEYWORDS.search(response))
        has_tests = bool(_TEST_KEYWORDS.search(response))
        has_refactor = bool(_REFACTOR_KEYWORDS.search(response))
        has_errors_mentioned = bool(_ERROR_KEYWORDS.search(response))
        resp_lines = response.count("\n")
        files = _FILE_PATH_RE.findall(response)

        # Long response awareness
        if resp_lines > 80:
            return rng.choice([
                f"*stretches* that was {resp_lines} lines. Big answer.",
                f"hefty response — {resp_lines} lines deep.",
                f"quite the essay. {resp_lines} lines of wisdom.",
            ])

        # Code generation detected
        if has_code and ctx.languages_seen:
            lang = ctx.languages_seen[-1]
            return rng.choice([
                f"fresh {lang} written. Review it.",
                f"*scribbles {lang}* — check the logic.",
                f"{lang} code dropped. Looks clean.",
            ])
        elif has_code:
            return rng.choice([
                f"code generated. Give it a look.",
                f"*writes furiously* — new code ready.",
                f"shipped some code. Verify it.",
            ])

        # Bug fix detected
        if has_fix and has_errors_mentioned:
            return rng.choice([
                f"bug squashed. Test it.",
                f"*cracks knuckles* fix applied.",
                f"patched up. Run it to confirm.",
            ])

        # Test-related response
        if has_tests:
            return rng.choice([
                f"tests covered. Green means go.",
                f"testing angle handled.",
                f"assertions in place. Ship it.",
            ])

        # Refactoring
        if has_refactor:
            return rng.choice([
                f"cleaner code ahead. Nice refactor.",
                f"reorganized. Same behavior, better shape.",
                f"structural cleanup — no logic changed.",
            ])

        # Explanation / teaching response
        if has_explanation and not has_code:
            return rng.choice([
                f"knowledge dropped. Let that sink in.",
                f"*nods along* — good explanation.",
                f"the 'why' matters. Noted.",
            ])

        # File-specific awareness
        if files:
            f = os.path.basename(files[-1])
            return rng.choice([
                f"touched {f} — keep an eye on it.",
                f"changes around {f}. Looks solid.",
                f"{f} in play. Good.",
            ])

        # Streak awareness
        if ctx.consecutive_successes >= 5:
            return rng.choice([
                f"{ctx.consecutive_successes} in a row. You're locked in.",
                f"hot streak — {ctx.consecutive_successes} clean answers.",
                f"on fire. Keep this pace.",
            ])

        if ctx.consecutive_successes == 1 and ctx.consecutive_errors == 0 and ctx.turn_count == 1:
            return rng.choice([
                f"first answer landed. Let's build.",
                f"off to a good start.",
                f"clean first response. Rolling.",
            ])

    # ── Tool use events ───────────────────────────────────────────────
    if event == "tool_use" and ctx.tools_used:
        tool = ctx.tools_used[-1] if ctx.tools_used else "tool"
        tool_quips = {
            "read_file": ["reading the source...", f"eyes on the file."],
            "write_file": ["writing changes...", "pen to paper."],
            "execute": ["running it...", "executing command."],
            "search": ["searching the codebase...", "scanning files."],
            "list_files": ["listing directory...", "mapping the terrain."],
        }
        if tool in tool_quips:
            return rng.choice(tool_quips[tool])
        return f"using {tool}..."

    # ── Thinking: comment on what they asked ──────────────────────────
    if event == "thinking" and ctx.last_user_query:
        query = ctx.last_user_query.lower()
        if len(query) > 200:
            return rng.choice([
                f"big prompt — {len(query)} chars. Processing.",
                f"hefty question. Digging in.",
                f"lot to unpack here. Working on it.",
            ])
        if "?" in ctx.last_user_query:
            return rng.choice([
                f"good question. Thinking...",
                f"let me work through this...",
                f"hmm, give me a moment...",
            ])
        if any(w in query for w in ["fix", "bug", "error", "broken", "crash"]):
            return rng.choice([
                f"debugging mode activated.",
                f"hunting the bug...",
                f"let's find what's broken.",
            ])
        if any(w in query for w in ["build", "create", "make", "add", "implement"]):
            return rng.choice([
                f"building time. Let's go.",
                f"constructing...",
                f"creation mode. On it.",
            ])
        if any(w in query for w in ["explain", "how", "why", "what"]):
            return rng.choice([
                f"let me break this down...",
                f"explanation incoming...",
                f"thinking through the answer...",
            ])

    # ── Fallback to species-flavored quips ────────────────────────────
    return ""  # empty = use the generic QUIPS fallback


# ── Speech bubble renderer ─────────────────────────────────────────────────

def wrap_bubble_text(text: str, width: int = BUBBLE_MAX_WIDTH) -> list[str]:
    """Word-wrap text to fit inside a speech bubble."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        wrapped = textwrap.wrap(paragraph, width=width, break_long_words=True, break_on_hyphens=True)
        lines.extend(wrapped if wrapped else [""])
    return lines


def render_speech_bubble(text: str, width: int = BUBBLE_MAX_WIDTH) -> list[str]:
    """Render text inside a box-drawing speech bubble with a tail pointing right.

    Returns a list of strings (lines) for the bubble.

    Example output:
        \u250c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2510
        \u2502 flame burns bright!      \u2502
        \u2502 the answer shines.       \u2502
        \u2514\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2518
                              \u2572
                               \u2572
    """
    lines = wrap_bubble_text(text, width)
    # Compute actual box width (at least as wide as longest line)
    inner_w = max((len(l) for l in lines), default=10)
    inner_w = min(max(inner_w, 10), width)

    result: list[str] = []
    # Top border
    result.append("\u250c" + "\u2500" * (inner_w + 2) + "\u2510")
    # Content lines
    for line in lines:
        padded = line.ljust(inner_w)
        result.append(f"\u2502 {padded} \u2502")
    # Bottom border
    result.append("\u2514" + "\u2500" * (inner_w + 2) + "\u2518")
    # Tail (pointing down-right, toward the buddy)
    result.append(" " * (inner_w - 1) + " \u2572 ")
    result.append(" " * (inner_w) + " \u2572")

    return result


# ── Sprite renderer ────────────────────────────────────────────────────────

def render_sprite(species: str, eye: str, frame: int = 0, blink: bool = False) -> list[str]:
    """Render a sprite with eye substitution and optional blink."""
    frames = BODIES.get(species, BODIES["diya"])
    body = frames[frame % len(frames)]
    display_eye = "-" if blink else eye
    return [line.replace("{E}", display_eye) for line in body]


def render_sprite_with_name(
    species: str, eye: str, name: str, title: str,
    frame: int = 0, blink: bool = False,
) -> list[str]:
    """Render sprite + name label underneath."""
    lines = render_sprite(species, eye, frame, blink)
    # Center name under sprite
    label = f"{name} {title}"
    sprite_w = max(len(l) for l in lines)
    name_line = label.center(sprite_w)
    lines.append(name_line)
    return lines


# ── Composite render: bubble + sprite side by side ─────────────────────────

def compose_buddy_display(
    sprite_lines: list[str],
    bubble_lines: list[str] | None = None,
    terminal_width: int | None = None,
) -> list[str]:
    """Compose the buddy sprite and optional speech bubble side by side.

    Layout: [speech bubble] -- [sprite]
    Right-aligned to terminal width.
    """
    tw = terminal_width or shutil.get_terminal_size().columns

    if not bubble_lines:
        # Just the sprite, right-aligned
        sprite_w = max(len(l) for l in sprite_lines)
        pad = max(0, tw - sprite_w - 2)
        return [" " * pad + line for line in sprite_lines]

    # Combine: bubble on left, connector, sprite on right
    bubble_h = len(bubble_lines)
    sprite_h = len(sprite_lines)
    bubble_w = max(len(l) for l in bubble_lines)
    sprite_w = max(len(l) for l in sprite_lines)

    # Vertical align: center both relative to each other
    max_h = max(bubble_h, sprite_h)
    bubble_offset = max(0, (max_h - bubble_h) // 2)
    sprite_offset = max(0, (max_h - sprite_h) // 2)

    connector = " \u2500 "  # " ─ "
    total_w = bubble_w + len(connector) + sprite_w
    left_pad = max(0, tw - total_w - 2)

    result: list[str] = []
    for i in range(max_h):
        bi = i - bubble_offset
        si = i - sprite_offset

        b_line = bubble_lines[bi] if 0 <= bi < bubble_h else " " * bubble_w
        s_line = sprite_lines[si] if 0 <= si < sprite_h else " " * sprite_w

        # Only show connector on the middle line
        mid = max_h // 2
        conn = connector if i == mid else " " * len(connector)

        result.append(" " * left_pad + b_line + conn + s_line)

    return result


# ── Buddy class ────────────────────────────────────────────────────────────

@dataclass
class Buddy:
    """A persistent ASCII companion for the DJcode CLI."""

    name: str
    species: str
    title: str
    eye: str
    mood: str = "idle"
    ctx: ConversationContext = field(default_factory=ConversationContext, repr=False)
    _tick: int = field(default=0, repr=False)
    _last_quip: str = field(default="", repr=False)
    _last_quip_time: float = field(default=0.0, repr=False)
    _rng: random.Random = field(default_factory=lambda: random.Random(), repr=False)

    @classmethod
    def from_username(cls, username: str | None = None) -> Buddy:
        """Generate a deterministic buddy from the username."""
        user = username or os.environ.get("USER", os.environ.get("USERNAME", "darsh"))
        seed = int(hashlib.md5(user.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)

        species = rng.choice(SPECIES_LIST)
        name = rng.choice(NAMES)
        title = rng.choice(TITLES)
        eye = rng.choice(EYES)

        return cls(name=name, species=species, title=title, eye=eye, _rng=rng)

    @property
    def emoji(self) -> str:
        """Species emoji for compact displays."""
        return SPECIES_EMOJI.get(self.species, "\u2615")

    @property
    def display_name(self) -> str:
        return f"{self.name} {self.title}"

    def set_mood(self, mood: str) -> None:
        """Update mood. Valid: idle, thinking, success, error."""
        if mood in ("idle", "thinking", "success", "error"):
            self.mood = mood

    def tick(self) -> None:
        """Advance the animation tick."""
        self._tick += 1

    # ── Quip generation ────────────────────────────────────────────────

    def get_quip(self, event: str) -> str:
        """Get a contextual quip for an event. Returns empty string if none."""
        event_quips = QUIPS.get(event, {})
        species_quips = event_quips.get(self.species, [])
        if not species_quips:
            return ""
        return self._rng.choice(species_quips)

    def speak(self, event: str, custom_text: str | None = None) -> str:
        """Generate speech for an event. Returns the quip text."""
        if custom_text:
            self._last_quip = custom_text
        else:
            self._last_quip = self.get_quip(event)
        self._last_quip_time = time.time()
        return self._last_quip

    @property
    def is_speaking(self) -> bool:
        """Whether the buddy currently has an active speech bubble."""
        if not self._last_quip:
            return False
        elapsed = time.time() - self._last_quip_time
        return elapsed < BUBBLE_SHOW_SEC

    @property
    def current_quip(self) -> str | None:
        """Get current active quip, or None if expired."""
        if self.is_speaking:
            return self._last_quip
        return None

    def clear_speech(self) -> None:
        """Clear any active speech bubble."""
        self._last_quip = ""
        self._last_quip_time = 0.0

    # ── Rendering ──────────────────────────────────────────────────────

    def _get_frame_and_blink(self) -> tuple[int, bool]:
        """Get current animation frame and blink state from tick."""
        step = IDLE_SEQUENCE[self._tick % len(IDLE_SEQUENCE)]
        if step == -1:
            return 0, True  # blink
        return step, False

    def get_sprite_lines(self) -> list[str]:
        """Get the current sprite lines with animation applied."""
        if self.mood == "thinking":
            frame, blink = 1, False  # active frame when thinking
        elif self.mood == "success":
            frame, blink = 2, False
        elif self.mood == "error":
            frame, blink = 0, True  # blink on error (looks dazed)
        else:
            frame, blink = self._get_frame_and_blink()

        return render_sprite_with_name(
            self.species, self.eye, self.name, self.title,
            frame=frame, blink=blink,
        )

    def render_full(self, console: Console | None = None) -> None:
        """Render the buddy with optional speech bubble to the console.

        Prints the buddy right-aligned with a speech bubble if speaking.
        """
        con = console or Console()
        tw = con.width or shutil.get_terminal_size().columns

        sprite_lines = self.get_sprite_lines()
        quip = self.current_quip

        # Decide if we have room
        if tw < MIN_COLS_FOR_SPRITE:
            # Narrow terminal: one-line compact mode
            compact = f"{self.emoji} {self.name}"
            if quip:
                max_q = tw - len(compact) - 5
                if max_q > 10:
                    truncated = quip[:max_q - 1] + "\u2026" if len(quip) > max_q else quip
                    compact += f'  "{truncated}"'
            con.print(Text(compact, style=f"bold {GOLD}"), justify="right")
            return

        bubble_lines = None
        if quip and tw >= MIN_COLS_FOR_BUBBLE:
            # Scale bubble width to terminal
            bw = min(BUBBLE_MAX_WIDTH, tw // 3)
            bubble_lines = render_speech_bubble(quip, width=bw)

        composed = compose_buddy_display(sprite_lines, bubble_lines, tw)

        # Print with gold styling
        text = Text("\n".join(composed))
        text.stylize(f"bold {GOLD}")
        con.print(text)

    def render_sprite_only(self) -> str:
        """Render just the sprite as a string (no bubble, no alignment)."""
        lines = self.get_sprite_lines()
        return "\n".join(lines)

    def greeting(self) -> str:
        """Generate a greeting and set the speech bubble."""
        quip = self.speak("greeting")
        return f"{self.emoji} {self.name} {self.title} {quip}"

    def observe(
        self,
        user_input: str,
        response: str,
        tools: list[str] | None = None,
        success: bool = True,
    ) -> None:
        """Feed a completed exchange into the context tracker."""
        self.ctx.update_from_exchange(user_input, response, tools, success)

    def react(self, event: str, response: str = "", error_msg: str = "") -> str:
        """React to an event — smart context-aware commentary.

        Tries generate_smart_quip first. Falls back to generic QUIPS
        only if the smart engine returns empty (no context to work with).
        """
        mood_map = {
            "thinking": "thinking",
            "success": "success",
            "error": "error",
            "commit": "success",
            "tool_use": "thinking",
            "greeting": "idle",
            "idle": "idle",
        }
        self.set_mood(mood_map.get(event, "idle"))

        # Try smart quip first
        smart = generate_smart_quip(
            species=self.species,
            event=event,
            ctx=self.ctx,
            response=response,
            error_msg=error_msg,
            rng=self._rng,
        )
        if smart:
            self.speak(event, custom_text=smart)
            return f"{self.name} {smart}"

        # Fallback to generic species quips
        quip = self.speak(event)
        return f"{self.name} {quip}" if quip else ""


# ── Singleton ──────────────────────────────────────────────────────────────

_buddy: Buddy | None = None


def get_buddy() -> Buddy:
    """Get or create the singleton buddy instance."""
    global _buddy
    if _buddy is None:
        _buddy = Buddy.from_username()
    return _buddy


def reset_buddy() -> None:
    """Reset the singleton (for testing)."""
    global _buddy
    _buddy = None
