"""DJcode Buddy — Fenwick-style ASCII companion with speech bubbles.

A persistent dharmic-themed ASCII creature that lives in your terminal.
Reacts to events with contextual speech bubbles. Has idle fidget animations.
Supports 3D depth illusion, glitch effects, enhanced blink cycles,
lifelike idle fidgets, and Rich-styled rendering.

Species: diya (oil lamp), cobra, lotus, peacock, om
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
from rich.style import Style
from rich.text import Text


# ── Constants ──────────────────────────────────────────────────────────────

GOLD = "#FFD700"
DIM_GOLD = "#4a3800"
GLITCH_RED = "#FF5F56"
SPRITE_WIDTH = 14          # max char width of any sprite
BUBBLE_MAX_WIDTH = 32      # inner text width of speech bubble
BUBBLE_SHOW_SEC = 8.0      # how long bubble stays visible
MIN_COLS_FOR_SPRITE = 60   # collapse below this terminal width
MIN_COLS_FOR_BUBBLE = 80   # no bubble below this width

# Idle animation: indices into frames. -1 = blink
IDLE_SEQUENCE = [0, 0, 0, 0, 1, 0, 0, 0, -1, 0, 0, 2, 0, 0, 0]

# ── Enhanced blink frames ─────────────────────────────────────────────────
# Frame 0: normal eyes, Frame 1: half-closed, Frame 2: closed, Frame 3: normal
BLINK_EYES: dict[int, str] = {
    0: "",       # use normal eye char
    1: "\u203e",  # ‾ half-closed
    2: "-",       # closed
    3: "",       # back to normal
}
BLINK_CYCLE_MIN = 8
BLINK_CYCLE_MAX = 12

# Glitch block characters
GLITCH_CHARS = list("▓░▒█")

# Species-specific special idle animations
SPECIES_SPECIAL_IDLE: dict[str, list[str]] = {
    "diya": ["flame_flicker"],
    "cobra": ["cobra_sway"],
    "lotus": ["petal_drop"],
    "peacock": ["fan_spread"],
    "om": ["om_pulse"],
}


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


# ── 3D ASCII Art Illusion ─────────────────────────────────────────────────

def render_3d_sprite(
    sprite_lines: list[str],
    shadow_offset: int = 1,
    shadow_char: str = "░",
) -> list[str]:
    """Add a 3D depth illusion to sprite lines.

    Creates a drop shadow shifted right+down by ``shadow_offset`` chars using
    dim block characters, and brightens the top-left edges for a highlight
    effect.  The returned lines are meant to be rendered with Rich styling
    (bold for front layer, dim for shadow layer).

    Args:
        sprite_lines: The raw sprite text lines.
        shadow_offset: How many chars to shift the shadow (right and down).
        shadow_char: Character used for the shadow fill.

    Returns:
        A list of strings where each line encodes *shadow* characters first
        (at the offset position) and then the front-layer characters on top.
        Non-space front chars overwrite shadow chars at the same position.
    """
    if not sprite_lines:
        return []

    height = len(sprite_lines)
    width = max(len(l) for l in sprite_lines)

    # Pad all lines to uniform width
    padded = [l.ljust(width) for l in sprite_lines]

    # Build a canvas that is (height + shadow_offset) x (width + shadow_offset)
    out_h = height + shadow_offset
    out_w = width + shadow_offset
    canvas: list[list[str]] = [[" "] * out_w for _ in range(out_h)]

    # 1. Lay down the shadow layer (shifted right+down)
    for row in range(height):
        for col in range(width):
            ch = padded[row][col]
            if ch != " ":
                sr, sc = row + shadow_offset, col + shadow_offset
                canvas[sr][sc] = shadow_char

    # 2. Lay down the front layer (overwrites shadow where they overlap)
    highlight_chars = {"/", "\\", "_", ",", ".", "'", '"', "*", "+", "~"}
    for row in range(height):
        for col in range(width):
            ch = padded[row][col]
            if ch != " ":
                canvas[row][col] = ch

    return ["".join(row) for row in canvas]


def render_3d_sprite_rich(
    sprite_lines: list[str],
    shadow_offset: int = 1,
    shadow_char: str = "░",
) -> Text:
    """Return a Rich Text object with 3D-styled sprite.

    Front layer is bold gold; shadow layer is dim gold; highlight edges
    (top-left facing chars) are extra bright.
    """
    if not sprite_lines:
        return Text("")

    height = len(sprite_lines)
    width = max(len(l) for l in sprite_lines)
    padded = [l.ljust(width) for l in sprite_lines]

    out_h = height + shadow_offset
    out_w = width + shadow_offset

    # Build char + style grids
    canvas: list[list[str]] = [[" "] * out_w for _ in range(out_h)]
    styles: list[list[str]] = [[""] * out_w for _ in range(out_h)]

    shadow_style = f"dim {DIM_GOLD}"
    front_style = f"bold {GOLD}"
    highlight_style = f"bold bright_white"

    highlight_chars = {"/", "\\", ",", ".", "'", '"', "*", "+"}

    # Shadow layer
    for row in range(height):
        for col in range(width):
            if padded[row][col] != " ":
                sr, sc = row + shadow_offset, col + shadow_offset
                canvas[sr][sc] = shadow_char
                styles[sr][sc] = shadow_style

    # Front layer
    for row in range(height):
        for col in range(width):
            ch = padded[row][col]
            if ch != " ":
                canvas[row][col] = ch
                # Top-left edge highlight for first non-space char in row
                # or chars that are structural edges
                if ch in highlight_chars and (row == 0 or col == 0):
                    styles[row][col] = highlight_style
                else:
                    styles[row][col] = front_style

    result = Text()
    for ri, row in enumerate(canvas):
        if ri > 0:
            result.append("\n")
        for ci, ch in enumerate(row):
            style = styles[ri][ci]
            if style:
                result.append(ch, style=style)
            else:
                result.append(ch)
    return result


# ── Glitch Effect ─────────────────────────────────────────────────────────

def glitch_sprite(
    sprite_lines: list[str],
    rng: random.Random | None = None,
    intensity: float = 0.3,
) -> list[str]:
    """Apply a glitch distortion to sprite lines.

    Randomly:
    - Shifts 1-2 lines horizontally by 1-3 chars
    - Replaces random chars with block characters (▓░▒█)
    - Duplicates a random line with slight shift

    Args:
        sprite_lines: The sprite text lines to glitch.
        rng: Random instance for deterministic glitching.
        intensity: 0.0-1.0 how aggressive the glitch is.

    Returns:
        A new list of glitched sprite lines. Original is not mutated.
    """
    if not sprite_lines:
        return []

    rng = rng or random.Random()
    result = [list(line) for line in sprite_lines]
    width = max(len(line) for line in sprite_lines)

    # Pad all to same width
    for i in range(len(result)):
        while len(result[i]) < width:
            result[i].append(" ")

    # 1. Horizontal line shift (1-2 lines)
    num_shifts = rng.randint(1, 2)
    for _ in range(num_shifts):
        line_idx = rng.randint(0, len(result) - 1)
        shift = rng.randint(1, 3) * rng.choice([-1, 1])
        line = result[line_idx]
        if shift > 0:
            result[line_idx] = [" "] * shift + line[:width - shift]
        else:
            result[line_idx] = line[abs(shift):] + [" "] * abs(shift)

    # 2. Random char replacement with glitch blocks
    num_replacements = max(1, int(width * len(result) * intensity * 0.1))
    for _ in range(num_replacements):
        r = rng.randint(0, len(result) - 1)
        c = rng.randint(0, len(result[r]) - 1)
        if result[r][c] != " ":
            result[r][c] = rng.choice(GLITCH_CHARS)

    # 3. Duplicate a random line (with slight shift) — insert it adjacent
    if len(result) > 2 and rng.random() < intensity:
        src = rng.randint(0, len(result) - 1)
        dup = list(result[src])
        # Slight shift
        shift = rng.choice([-1, 1])
        if shift > 0:
            dup = [" "] + dup[:width - 1]
        else:
            dup = dup[1:] + [" "]
        insert_at = min(src + 1, len(result))
        result.insert(insert_at, dup)

    return ["".join(row) for row in result]


def glitch_sprite_rich(
    sprite_lines: list[str],
    rng: random.Random | None = None,
    intensity: float = 0.3,
) -> Text:
    """Return a Rich Text with glitch styling — glitch chars in red."""
    glitched = glitch_sprite(sprite_lines, rng=rng, intensity=intensity)
    result = Text()
    for li, line in enumerate(glitched):
        if li > 0:
            result.append("\n")
        for ch in line:
            if ch in GLITCH_CHARS:
                result.append(ch, style=f"bold {GLITCH_RED}")
            else:
                result.append(ch, style=f"bold {GOLD}")
    return result


# ── Enhanced Blink Animation ──────────────────────────────────────────────

def get_blink_eye(eye: str, blink_frame: int) -> str:
    """Return the eye character for a given blink animation frame.

    Blink cycle:
      Frame 0 — normal eyes
      Frame 1 — half-closed (‾ or ¬)
      Frame 2 — closed (-)
      Frame 3 — back to normal
    """
    if blink_frame == 1:
        return "\u203e"  # ‾ half-closed
    elif blink_frame == 2:
        return "-"        # closed
    else:
        return eye        # frame 0 and 3: normal


# ── Idle Fidget Micro-movements ───────────────────────────────────────────

def apply_micro_shift(sprite_lines: list[str], direction: int) -> list[str]:
    """Shift entire sprite 1 char left or right for micro-movement.

    Args:
        sprite_lines: The sprite text lines.
        direction: -1 for left, +1 for right.

    Returns:
        Shifted sprite lines.
    """
    if direction > 0:
        return [" " + line[:-1] if len(line) > 1 else " " for line in sprite_lines]
    elif direction < 0:
        return [line[1:] + " " if len(line) > 1 else " " for line in sprite_lines]
    return list(sprite_lines)


def apply_breathing(sprite_lines: list[str], phase: int) -> list[str]:
    """Simulate breathing by adding/removing a blank line.

    phase 0 = normal, phase 1 = inhale (stretch — add spacer), phase 2 = exhale (back to normal).
    """
    if phase == 1 and len(sprite_lines) >= 2:
        # Insert a thin spacer line between top and body
        mid = len(sprite_lines) // 2
        width = max(len(l) for l in sprite_lines)
        spacer = " " * width
        return sprite_lines[:mid] + [spacer] + sprite_lines[mid:]
    return list(sprite_lines)


def apply_species_special(
    sprite_lines: list[str],
    species: str,
    animation: str,
    rng: random.Random | None = None,
) -> list[str]:
    """Apply a species-specific special idle animation.

    These are subtle single-frame modifications:
    - diya flame_flicker: randomize the flame tip char
    - cobra cobra_sway: shift top 2 lines slightly
    - lotus petal_drop: replace a petal char with a falling dot
    - peacock fan_spread: widen the top fan line
    - om om_pulse: add radiating chars around the om symbol
    """
    rng = rng or random.Random()
    result = list(sprite_lines)

    if animation == "flame_flicker" and species == "diya" and result:
        flame_chars = [",", "'", "`", ".", "*", "+"]
        # Replace the flame tip character (first line, center area)
        line = list(result[0])
        for i, ch in enumerate(line):
            if ch in (",", "'", "`", ".", "*", "+"):
                line[i] = rng.choice(flame_chars)
        result[0] = "".join(line)

    elif animation == "cobra_sway" and species == "cobra" and len(result) >= 2:
        shift = rng.choice([-1, 1])
        for idx in range(min(2, len(result))):
            result[idx] = apply_micro_shift([result[idx]], shift)[0]

    elif animation == "petal_drop" and species == "lotus" and len(result) >= 3:
        # Add a falling petal dot below the lotus
        width = max(len(l) for l in result)
        col = rng.randint(width // 3, 2 * width // 3)
        drop_line = " " * col + "." + " " * (width - col - 1)
        result.append(drop_line)

    elif animation == "fan_spread" and species == "peacock" and result:
        # Widen the fan line by adding * at edges
        line = result[0]
        result[0] = "*" + line[1:-1] + "*" if len(line) > 2 else line

    elif animation == "om_pulse" and species == "om" and result:
        # Add radiating dots on the om line
        line = list(result[0])
        for i, ch in enumerate(line):
            if ch == " " and rng.random() < 0.3:
                line[i] = rng.choice([".", "+", "*"])
        result[0] = "".join(line)

    return result


# ── Rich-Styled Rendering ─────────────────────────────────────────────────

def render_rich_styled(
    sprite_lines: list[str],
    name: str,
    title: str,
    bubble_text: str | None = None,
    bubble_width: int = BUBBLE_MAX_WIDTH,
    is_glitched: bool = False,
    is_3d: bool = False,
    eye_char: str = ".",
) -> Text:
    """Return a fully Rich-styled Text object with per-character styling.

    Styling rules:
    - Species body: bold gold (#FFD700)
    - Eyes: bright white
    - Shadow/depth: dim (#4a3800)
    - Name: italic gold
    - Speech bubble border: dim white
    - Speech bubble text: normal white
    - Glitch chars: red (#FF5F56)
    """
    body_style = Style(bold=True, color=GOLD)
    eye_style = Style(bold=True, color="bright_white")
    shadow_style = Style(dim=True, color=DIM_GOLD)
    name_style = Style(italic=True, color=GOLD)
    bubble_border_style = Style(dim=True, color="white")
    bubble_text_style = Style(color="white")
    glitch_style = Style(bold=True, color=GLITCH_RED)

    result = Text()

    # Render speech bubble if present
    if bubble_text:
        bubble_lines = render_speech_bubble(bubble_text, width=bubble_width)
        for line in bubble_lines:
            for ch in line:
                if ch in ("┌", "┐", "└", "┘", "─", "│", "╲"):
                    result.append(ch, style=bubble_border_style)
                else:
                    result.append(ch, style=bubble_text_style)
            result.append("\n")

    # Render sprite body
    for li, line in enumerate(sprite_lines):
        if li > 0:
            result.append("\n")
        for ch in line:
            if ch in GLITCH_CHARS and is_glitched:
                result.append(ch, style=glitch_style)
            elif ch == "░" and is_3d:
                result.append(ch, style=shadow_style)
            elif ch == eye_char:
                result.append(ch, style=eye_style)
            elif ch == "-" and is_glitched:
                result.append(ch, style=glitch_style)
            else:
                result.append(ch, style=body_style)

    # Render name line
    sprite_w = max((len(l) for l in sprite_lines), default=14)
    label = f"{name} {title}"
    name_line = label.center(sprite_w)
    result.append("\n")
    result.append(name_line, style=name_style)

    return result


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
    """A persistent ASCII companion for the DJcode CLI.

    Supports optional 3D depth illusion, glitch distortion, enhanced
    multi-frame blink cycles, lifelike idle fidgets (micro-shifts,
    breathing, species-specific specials), and Rich-styled rendering.
    """

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

    # ── Enhanced animation state ──────────────────────────────────────
    _glitch_active: bool = field(default=False, repr=False)
    _glitch_intensity: float = field(default=0.3, repr=False)
    _blink_frame: int = field(default=0, repr=False)
    _blink_counter: int = field(default=0, repr=False)
    _next_blink_at: int = field(default=10, repr=False)
    _breathing_phase: int = field(default=0, repr=False)
    _micro_shift: int = field(default=0, repr=False)
    _enable_3d: bool = field(default=False, repr=False)
    _enable_fidgets: bool = field(default=True, repr=False)

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
        """Advance the animation tick with enhanced blink and fidget logic."""
        self._tick += 1

        # ── Enhanced blink cycle ──────────────────────────────────────
        self._blink_counter += 1
        if self._blink_frame > 0:
            # Currently in a blink — advance through frames 1→2→3→0
            self._blink_frame += 1
            if self._blink_frame > 3:
                self._blink_frame = 0
                # Schedule next blink randomly
                self._next_blink_at = self._blink_counter + self._rng.randint(
                    BLINK_CYCLE_MIN, BLINK_CYCLE_MAX
                )
        elif self._blink_counter >= self._next_blink_at:
            # Time to start a new blink
            self._blink_frame = 1

        # ── Idle fidget state updates ─────────────────────────────────
        if self._enable_fidgets and self.mood == "idle":
            # Micro-shift: occasional 1-char drift
            if self._tick % 7 == 0:
                self._micro_shift = self._rng.choice([-1, 0, 0, 1])
            elif self._tick % 5 == 0:
                self._micro_shift = 0  # return to center

            # Breathing cycle: 0→1→0 every ~12 ticks
            cycle_pos = self._tick % 12
            if cycle_pos < 4:
                self._breathing_phase = 0
            elif cycle_pos < 8:
                self._breathing_phase = 1
            else:
                self._breathing_phase = 0

        # ── Auto-reset glitch after one frame ─────────────────────────
        if self._glitch_active:
            self._glitch_active = False

    # ── Glitch trigger ────────────────────────────────────────────────

    def glitch(self, intensity: float = 0.3) -> None:
        """Trigger a one-frame glitch distortion.

        Call this when the buddy reacts to errors or unexpected events.
        The glitch auto-resets after the next tick() call.
        """
        self._glitch_active = True
        self._glitch_intensity = max(0.0, min(1.0, intensity))

    # ── 3D mode toggle ────────────────────────────────────────────────

    def set_3d(self, enabled: bool = True) -> None:
        """Enable or disable the 3D depth illusion on the sprite."""
        self._enable_3d = enabled

    def set_fidgets(self, enabled: bool = True) -> None:
        """Enable or disable idle fidget animations."""
        self._enable_fidgets = enabled

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

    def _get_enhanced_blink_eye(self) -> str:
        """Return the current eye char based on the enhanced blink cycle."""
        if self._blink_frame > 0:
            return get_blink_eye(self.eye, self._blink_frame)
        return self.eye

    def get_sprite_lines(self) -> list[str]:
        """Get the current sprite lines with animation applied.

        Applies (in order): frame selection, enhanced blink, idle fidgets
        (micro-shift, breathing, species specials), glitch, and 3D depth.
        """
        if self.mood == "thinking":
            frame, blink = 1, False
        elif self.mood == "success":
            frame, blink = 2, False
        elif self.mood == "error":
            frame, blink = 0, True
        else:
            frame, blink = self._get_frame_and_blink()

        # Use enhanced blink eye instead of simple on/off
        if self._blink_frame > 0:
            display_eye = self._get_enhanced_blink_eye()
            # Render with custom eye (not the simple blink=True path)
            frames = BODIES.get(self.species, BODIES["diya"])
            body = frames[frame % len(frames)]
            lines = [line.replace("{E}", display_eye) for line in body]
            # Add name label
            label = f"{self.name} {self.title}"
            sprite_w = max(len(l) for l in lines)
            lines.append(label.center(sprite_w))
        else:
            lines = render_sprite_with_name(
                self.species, self.eye, self.name, self.title,
                frame=frame, blink=blink,
            )

        # ── Apply idle fidgets (only in idle mood) ────────────────────
        if self._enable_fidgets and self.mood == "idle":
            # Micro-shift
            if self._micro_shift != 0:
                # Shift only the body lines, not the name label
                body = lines[:-1]
                name_line = lines[-1]
                body = apply_micro_shift(body, self._micro_shift)
                lines = body + [name_line]

            # Breathing
            if self._breathing_phase == 1:
                body = lines[:-1]
                name_line = lines[-1]
                body = apply_breathing(body, self._breathing_phase)
                lines = body + [name_line]

            # Species-specific special (rare — ~5% chance per tick)
            specials = SPECIES_SPECIAL_IDLE.get(self.species, [])
            if specials and self._rng.random() < 0.05:
                anim = self._rng.choice(specials)
                body = lines[:-1]
                name_line = lines[-1]
                body = apply_species_special(body, self.species, anim, self._rng)
                lines = body + [name_line]

        # ── Apply glitch (one-frame, explicit trigger) ────────────────
        if self._glitch_active:
            body = lines[:-1]
            name_line = lines[-1]
            body = glitch_sprite(body, rng=self._rng, intensity=self._glitch_intensity)
            lines = body + [name_line]

        # ── Apply 3D depth (explicit toggle) ──────────────────────────
        if self._enable_3d:
            body = lines[:-1]
            name_line = lines[-1]
            body = render_3d_sprite(body)
            # Re-center name under the now-wider 3D sprite
            sprite_w = max(len(l) for l in body) if body else 14
            lines = body + [name_line.strip().center(sprite_w)]

        return lines

    def render_full(self, console: Console | None = None) -> None:
        """Render the buddy with optional speech bubble to the console.

        Prints the buddy right-aligned with a speech bubble if speaking.
        Uses basic gold styling. For per-character Rich styling, use
        ``render_rich_styled()`` instead.
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

    def render_rich_styled_text(self, console: Console | None = None) -> Text:
        """Return a Rich Text object with full per-character styling.

        Applies all active effects (3D, glitch, enhanced blink) with
        proper color coding:
        - Body: bold gold
        - Eyes: bright white
        - Shadow (3D): dim gold
        - Glitch chars: red
        - Name: italic gold
        - Bubble borders: dim white
        - Bubble text: white
        """
        sprite_lines = self.get_sprite_lines()
        quip = self.current_quip

        con = console or Console()
        tw = con.width or shutil.get_terminal_size().columns
        bw = min(BUBBLE_MAX_WIDTH, tw // 3) if quip else BUBBLE_MAX_WIDTH

        return render_rich_styled(
            sprite_lines=sprite_lines,
            name=self.name,
            title=self.title,
            bubble_text=quip,
            bubble_width=bw,
            is_glitched=self._glitch_active,
            is_3d=self._enable_3d,
            eye_char=self.eye,
        )

    def render_rich_full(self, console: Console | None = None) -> None:
        """Render with full Rich per-character styling to the console.

        Like ``render_full()`` but uses ``render_rich_styled_text()`` for
        precise per-character color control instead of blanket gold.
        """
        con = console or Console()
        styled = self.render_rich_styled_text(console=con)
        con.print(styled, justify="right")

    def render_sprite_only(self) -> str:
        """Render just the sprite as a string (no bubble, no alignment)."""
        lines = self.get_sprite_lines()
        return "\n".join(lines)

    def render_3d(self, console: Console | None = None) -> None:
        """Render the buddy with 3D depth illusion.

        Convenience method — temporarily enables 3D, renders, then
        restores the previous 3D state.
        """
        prev = self._enable_3d
        self._enable_3d = True
        self.render_full(console=console)
        self._enable_3d = prev

    def render_glitched(self, console: Console | None = None, intensity: float = 0.3) -> None:
        """Render one glitch frame immediately.

        Convenience method — triggers glitch, renders, does NOT auto-reset
        (that happens on next tick).
        """
        self.glitch(intensity=intensity)
        self.render_full(console=console)

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

        On error events, also triggers a glitch effect automatically.
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

        # Auto-glitch on errors
        if event == "error":
            self.glitch(intensity=0.4)

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
