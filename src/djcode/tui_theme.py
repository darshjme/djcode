"""DJcode Textual TUI Theme — Gold/Black aesthetic.

Complete CSS theme for the split-pane Textual interface.
Matches the DJcode brand: dark backgrounds, gold highlights,
clean typography, and high-contrast readability.
"""

from __future__ import annotations

# ── Color palette ─────────────────────────────────────────────────────────

GOLD = "#FFD700"
DIM_GOLD = "#B88F00"
DARK_GOLD = "#4A3800"
BG_PRIMARY = "#0A0A0A"
BG_SECONDARY = "#111111"
BG_PANEL = "#0D0D0D"
BORDER = "#333333"
BORDER_FOCUS = "#FFD700"
DIM_TEXT = "#666666"
MUTED_TEXT = "#888888"
SUCCESS = "#4ADE80"
ERROR = "#FF5F56"
WARNING = "#FFBD2E"
INFO = "#61AFEF"
THINKING = "#9B59B6"
PLAN_MODE = "#FF00FF"
ACT_MODE = "#00FF00"

# ── Main CSS ──────────────────────────────────────────────────────────────

DJCODE_CSS = """
Screen {
    background: #0A0A0A;
    color: #CCCCCC;
}

/* ── Header ────────────────────────────────────────────────── */

Header {
    background: #111111;
    color: #FFD700;
    dock: top;
    height: 1;
}

HeaderTitle {
    color: #FFD700;
    text-style: bold;
}

/* ── Footer ────────────────────────────────────────────────── */

Footer {
    background: #111111;
    color: #888888;
    dock: bottom;
}

FooterKey {
    background: #1A1A1A;
    color: #FFD700;
}

/* ── Layout containers ─────────────────────────────────────── */

#main-layout {
    height: 1fr;
    width: 100%;
}

#chat-panel {
    width: 65%;
    border: solid #333333;
    border-title-color: #FFD700;
    border-title-style: bold;
    background: #0A0A0A;
}

#chat-panel:focus-within {
    border: solid #FFD700;
}

#side-panel {
    width: 35%;
    border: solid #333333;
    border-title-color: #FFD700;
    border-title-style: bold;
    background: #0D0D0D;
}

#side-panel:focus-within {
    border: solid #FFD700;
}

/* ── Chat log ──────────────────────────────────────────────── */

#chat-log {
    height: 1fr;
    background: #0A0A0A;
    color: #CCCCCC;
    scrollbar-color: #333333;
    scrollbar-color-hover: #FFD700;
    scrollbar-color-active: #FFD700;
    padding: 0 1;
}

/* ── Agent panel sections ──────────────────────────────────── */

#agent-header {
    height: 3;
    background: #111111;
    color: #FFD700;
    text-style: bold;
    padding: 0 1;
    border-bottom: solid #333333;
    content-align: left middle;
}

#agent-log {
    height: 1fr;
    background: #0D0D0D;
    color: #AAAAAA;
    scrollbar-color: #333333;
    scrollbar-color-hover: #FFD700;
    scrollbar-color-active: #FFD700;
    padding: 0 1;
}

#stats-bar {
    height: 3;
    background: #111111;
    color: #888888;
    padding: 0 1;
    border-top: solid #333333;
    content-align: left middle;
}

/* ── Input ─────────────────────────────────────────────────── */

#prompt-input {
    dock: bottom;
    height: 3;
    background: #111111;
    color: #FFD700;
    border-top: solid #333333;
    padding: 0 1;
}

#prompt-input:focus {
    border-top: solid #FFD700;
}

Input > .input--placeholder {
    color: #555555;
}

Input > .input--cursor {
    color: #FFD700;
    text-style: bold;
}

/* ── Help overlay ──────────────────────────────────────────── */

#help-overlay {
    align: center middle;
    background: rgba(0, 0, 0, 0.85);
}

#help-panel {
    width: 70;
    height: auto;
    max-height: 80%;
    background: #111111;
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
    color: #CCCCCC;
    height: auto;
    max-height: 100%;
}

/* ── Agents overlay ────────────────────────────────────────── */

#agents-overlay {
    align: center middle;
    background: rgba(0, 0, 0, 0.85);
}

#agents-panel {
    width: 80;
    height: auto;
    max-height: 80%;
    background: #111111;
    border: double #FFD700;
    padding: 1 2;
}

/* ── Utility classes ───────────────────────────────────────── */

.gold {
    color: #FFD700;
}

.dim {
    color: #666666;
}

.muted {
    color: #888888;
}

.success {
    color: #4ADE80;
}

.error {
    color: #FF5F56;
}

.warning {
    color: #FFBD2E;
}

.thinking {
    color: #9B59B6;
    text-style: italic;
}

.tool-name {
    color: #61AFEF;
    text-style: bold;
}

.user-msg {
    color: #FFD700;
}

.assistant-msg {
    color: #CCCCCC;
}

.system-msg {
    color: #666666;
    text-style: italic;
}

.separator {
    color: #333333;
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
    "BORDER",
    "BORDER_FOCUS",
    "DIM_TEXT",
    "MUTED_TEXT",
    "SUCCESS",
    "ERROR",
    "WARNING",
    "INFO",
    "THINKING",
    "PLAN_MODE",
    "ACT_MODE",
]
