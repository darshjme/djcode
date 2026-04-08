"""DJcode Textual TUI Theme — Professional dark theme with gold accent.

KiloCode-inspired dark aesthetic with DJcode's signature gold identity.
Layered neutral backgrounds, calibrated text hierarchy, and polished
interactive states for a production-grade terminal experience.
"""

from __future__ import annotations

# ── Color palette ─────────────────────────────────────────────────────────

# Brand
GOLD = "#FFD700"
DIM_GOLD = "#B8960F"
DARK_GOLD = "#3D2E00"

# Backgrounds (layered depth)
BG_PRIMARY = "#101010"
BG_SECONDARY = "#1a1a1a"
BG_PANEL = "#141414"

# Text hierarchy
TEXT_STRONG = "#ededed"
TEXT_BASE = "#a0a0a0"
TEXT_DIM = "#6f6f6f"
DIM_TEXT = TEXT_DIM  # compat alias
MUTED_TEXT = TEXT_BASE  # compat alias

# Borders
BORDER = "#2a2a2a"
BORDER_FOCUS = "#FFD700"

# Interactive
LINK = "#3b82f6"

# Status
SUCCESS = "#12c905"
ERROR = "#fc533a"
WARNING = "#fcd53a"
INFO = "#a78bfa"
THINKING = "#c084fc"

# Modes
PLAN_MODE = "#a78bfa"
ACT_MODE = "#12c905"

# Syntax highlighting
SYN_STRING = "#00ceb9"
SYN_PRIMITIVE = "#ffba92"
SYN_PROPERTY = "#ed6dc8"
SYN_TYPE = "#a5d6ff"

# ── Main CSS ──────────────────────────────────────────────────────────────

DJCODE_CSS = """

/* ================================================================
   DJcode TUI Theme
   Professional dark theme — gold accent, neutral layers
   ================================================================ */

/* ── Screen ───────────────────────────────────────────────────── */

Screen {
    background: #101010;
    color: #a0a0a0;
}

/* ── Header ───────────────────────────────────────────────────── */

Header {
    background: #141414;
    color: #FFD700;
    dock: top;
    height: 1;
}

HeaderTitle {
    color: #FFD700;
    text-style: bold;
}

/* ── Footer ───────────────────────────────────────────────────── */

Footer {
    background: #141414;
    color: #6f6f6f;
    dock: bottom;
}

FooterKey {
    background: #1a1a1a;
    color: #FFD700;
}

/* ── Status bar ───────────────────────────────────────────────── */

#status-bar {
    dock: bottom;
    height: 1;
    background: #1a1a1a;
    color: #a0a0a0;
    padding: 0 1;
    border-top: solid #2a2a2a;
    content-align: left middle;
}

/* ── Layout containers ────────────────────────────────────────── */

#main-layout {
    height: 1fr;
    width: 100%;
}

/* ── Chat panel (65%) ─────────────────────────────────────────── */

#chat-panel {
    width: 65%;
    border: solid #2a2a2a;
    border-title-color: #FFD700;
    border-title-style: bold;
    background: #101010;
}

#chat-panel:focus-within {
    border: solid #FFD700;
}

/* ── Side panel (35%) ─────────────────────────────────────────── */

#side-panel {
    width: 35%;
    border: solid #2a2a2a;
    border-title-color: #FFD700;
    border-title-style: bold;
    background: #141414;
    padding: 0;
}

#side-panel:focus-within {
    border: solid #FFD700;
}

/* ── SidePanel tabs ───────────────────────────────────────────── */

SidePanel TabbedContent {
    height: 100%;
    background: #141414;
}

SidePanel ContentSwitcher {
    height: 1fr;
    background: #141414;
}

SidePanel TabPane {
    padding: 0;
    height: 1fr;
    background: #141414;
}

SidePanel Tabs {
    background: #1a1a1a;
    dock: top;
    height: 3;
}

SidePanel Tab {
    background: #1a1a1a;
    color: #6f6f6f;
    padding: 0 2;
    text-style: bold;
    min-width: 8;
}

SidePanel Tab:hover {
    background: #2a2a2a;
    color: #ededed;
}

SidePanel Tab.-active {
    background: #FFD700;
    color: #101010;
    text-style: bold;
}

SidePanel Underline {
    color: #FFD700;
}

/* ── Chat log ─────────────────────────────────────────────────── */

#chat-log {
    height: 1fr;
    background: #101010;
    color: #a0a0a0;
    scrollbar-color: #2a2a2a;
    scrollbar-color-hover: #FFD700;
    scrollbar-color-active: #FFD700;
    padding: 0 1;
}

/* ── Agent panel sections ─────────────────────────────────────── */

#agent-header {
    height: 3;
    background: #1a1a1a;
    color: #FFD700;
    text-style: bold;
    padding: 0 1;
    border-bottom: solid #2a2a2a;
    content-align: left middle;
}

#agent-log {
    height: 1fr;
    background: #141414;
    color: #a0a0a0;
    scrollbar-color: #2a2a2a;
    scrollbar-color-hover: #FFD700;
    scrollbar-color-active: #FFD700;
    padding: 0 1;
}

#stats-bar {
    height: 3;
    background: #1a1a1a;
    color: #6f6f6f;
    padding: 0 1;
    border-top: solid #2a2a2a;
    content-align: left middle;
}

/* ── Input ────────────────────────────────────────────────────── */

#cmd-suggest {
    dock: bottom;
    height: auto;
    max-height: 12;
    background: #141414;
    color: #e0e0e0;
    border: solid #2a2a2a;
    margin: 0 1;
    display: none;
}

#cmd-suggest:focus {
    border: solid #FFD700;
}

#cmd-suggest > .option-list--option-highlighted {
    background: #FFD700 20%;
    color: #FFD700;
}

#cmd-suggest > .option-list--option {
    padding: 0 1;
}

#prompt-input {
    dock: bottom;
    height: 3;
    background: #1a1a1a;
    color: #FFD700;
    border-top: solid #2a2a2a;
    padding: 0 1;
}

#prompt-input:focus {
    border-top: solid #FFD700;
}

Input > .input--placeholder {
    color: #6f6f6f;
}

Input > .input--cursor {
    color: #FFD700;
    text-style: bold;
}

/* ── Help overlay ─────────────────────────────────────────────── */

#help-overlay {
    align: center middle;
    background: rgba(0, 0, 0, 0.85);
}

#help-panel {
    width: 70;
    height: auto;
    max-height: 80%;
    background: #1a1a1a;
    border: double #FFD700;
    padding: 1 2;
}

#help-title {
    text-style: bold;
    color: #FFD700;
    text-align: center;
    margin-bottom: 1;
}

#help-content {
    color: #a0a0a0;
    height: auto;
    max-height: 100%;
}

/* ── Agents overlay ───────────────────────────────────────────── */

#agents-overlay {
    align: center middle;
    background: rgba(0, 0, 0, 0.85);
}

#agents-panel {
    width: 80;
    height: auto;
    max-height: 80%;
    background: #1a1a1a;
    border: double #FFD700;
    padding: 1 2;
}

/* ── Utility classes ──────────────────────────────────────────── */

.gold {
    color: #FFD700;
}

.dim {
    color: #6f6f6f;
}

.muted {
    color: #a0a0a0;
}

.strong {
    color: #ededed;
}

.link {
    color: #3b82f6;
    text-style: underline;
}

.success {
    color: #12c905;
}

.error {
    color: #fc533a;
}

.warning {
    color: #fcd53a;
}

.info {
    color: #a78bfa;
}

.thinking {
    color: #c084fc;
    text-style: italic;
}

.tool-name {
    color: #3b82f6;
    text-style: bold;
}

.user-msg {
    color: #FFD700;
}

.assistant-msg {
    color: #ededed;
}

.system-msg {
    color: #6f6f6f;
    text-style: italic;
}

.separator {
    color: #2a2a2a;
}

/* ── Syntax classes ───────────────────────────────────────────── */

.syn-string {
    color: #00ceb9;
}

.syn-primitive {
    color: #ffba92;
}

.syn-property {
    color: #ed6dc8;
}

.syn-type {
    color: #a5d6ff;
}
"""

__all__ = [
    "DJCODE_CSS",
    "GOLD",
    "DIM_GOLD",
    "DARK_GOLD",
    "BG_PRIMARY",
    "BG_SECONDARY",
    "BG_PANEL",
    "TEXT_STRONG",
    "TEXT_BASE",
    "TEXT_DIM",
    "BORDER",
    "BORDER_FOCUS",
    "DIM_TEXT",
    "MUTED_TEXT",
    "LINK",
    "SUCCESS",
    "ERROR",
    "WARNING",
    "INFO",
    "THINKING",
    "PLAN_MODE",
    "ACT_MODE",
    "SYN_STRING",
    "SYN_PRIMITIVE",
    "SYN_PROPERTY",
    "SYN_TYPE",
]
