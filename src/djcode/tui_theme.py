"""DJcode v4.0 Hacker TUI Theme -- Cyberpunk military command center aesthetic.

Matrix-meets-terminal dark theme with electric gold identity, neon green active
states, agent-tier glow accents, HUD-style borders, and scanline effects.
Optimized for dark terminals. Supports light terminals gracefully.
"""

from __future__ import annotations

# ── Color palette ─────────────────────────────────────────────────────────

# Brand / Primary
GOLD = "#FFD700"
DIM_GOLD = "#B8960F"
DARK_GOLD = "#3D2E00"
ELECTRIC_GOLD = "#FFD700"

# Backgrounds (deep black layered depth)
BG_PRIMARY = "#0A0A0A"
BG_SECONDARY = "#121212"
BG_PANEL = "#0E0E0E"
BG_HEADER = "#0A0A0A"
BG_INPUT = "#111111"

# Text hierarchy
TEXT_STRONG = "#E8E8E8"
TEXT_BASE = "#8A8A8A"
TEXT_DIM = "#555555"
DIM_TEXT = TEXT_DIM  # compat alias
MUTED_TEXT = TEXT_BASE  # compat alias

# Borders
BORDER = "#1E1E1E"
BORDER_FOCUS = "#FFD700"
BORDER_GLOW = "#FFD700"
BORDER_SUBTLE = "#1A1A1A"

# Interactive
LINK = "#2196F3"

# Status — vibrant cyberpunk palette
SUCCESS = "#00FF41"       # Matrix neon green
ERROR = "#FF1744"         # Blood red
WARNING = "#FF8C00"       # Amber
INFO = "#2196F3"          # Electric blue
THINKING = "#00BCD4"      # Cyan/teal for AI thinking

# Modes
PLAN_MODE = "#9C27B0"     # Purple — architecture mode
ACT_MODE = "#00FF41"      # Neon green — execution mode

# Agent tier accents
TIER_4_CONTROL = "#FFD700"   # Pure gold — Vyasa, Control tier
TIER_3_ENTERPRISE = "#2196F3"  # Electric blue — Enterprise tier
TIER_2_ARCHITECTURE = "#9C27B0"  # Purple — Architecture tier
TIER_1_EXECUTION = "#00FF41"   # Neon green — Execution tier

# Threat agents
THREAT_KAVACH = "#FF1744"
THREAT_VARUNA = "#FF6D00"
THREAT_MITRA = "#FF8C00"
THREAT_INDRA = "#D50000"

# Syntax highlighting (tuned for dark bg)
SYN_STRING = "#00E5CC"
SYN_PRIMITIVE = "#FFB74D"
SYN_PROPERTY = "#F06292"
SYN_TYPE = "#90CAF9"
SYN_KEYWORD = "#CE93D8"
SYN_COMMENT = "#555555"
SYN_FUNCTION = "#FFD700"
SYN_NUMBER = "#FF8A80"

# HUD elements
HUD_BORDER = "#333333"
HUD_ACTIVE = "#00FF41"
HUD_INACTIVE = "#1A1A1A"
SCANLINE = "rgba(0, 255, 65, 0.03)"
MATRIX_GREEN = "#00FF41"

# ── Main CSS ──────────────────────────────────────────────────────────────

DJCODE_CSS = """

/* ================================================================
   DJcode v4.0 — HACKER COMMAND CENTER THEME
   Cyberpunk terminal | Matrix aesthetic | Military HUD
   ================================================================ */

/* ── Screen ───────────────────────────────────────────────────── */

Screen {
    background: #0A0A0A;
    color: #8A8A8A;
}

/* ── Header — Military HUD bar ────────────────────────────────── */

Header {
    background: #0A0A0A;
    color: #FFD700;
    dock: top;
    height: 1;
    border-bottom: solid #1E1E1E;
}

HeaderTitle {
    color: #FFD700;
    text-style: bold;
}

/* ── Footer — Status telemetry strip ─────────────────────────── */

Footer {
    background: #0A0A0A;
    color: #555555;
    dock: bottom;
    border-top: solid #1E1E1E;
}

FooterKey {
    background: #111111;
    color: #00FF41;
}

FooterKey:hover {
    background: #1A1A1A;
    color: #FFD700;
}

/* ── Status bar — system telemetry ───────────────────────────── */

#status-bar {
    dock: bottom;
    height: 1;
    background: #0E0E0E;
    color: #555555;
    padding: 0 1;
    border-top: solid #1E1E1E;
    content-align: left middle;
}

/* ── Layout containers ────────────────────────────────────────── */

#main-layout {
    height: 1fr;
    width: 100%;
}

/* ── Chat panel (65%) — Command terminal ─────────────────────── */

#chat-panel {
    width: 65%;
    border: double #1E1E1E;
    border-title-color: #FFD700;
    border-title-style: bold;
    background: #0A0A0A;
}

#chat-panel:focus-within {
    border: double #FFD700;
}

/* ── Side panel (35%) — Intelligence dashboard ───────────────── */

#side-panel {
    width: 35%;
    border: double #1E1E1E;
    border-title-color: #00FF41;
    border-title-style: bold;
    background: #0E0E0E;
    padding: 0;
}

#side-panel:focus-within {
    border: double #00FF41;
}

/* ── SidePanel tabs — HUD navigation ─────────────────────────── */

SidePanel TabbedContent {
    height: 100%;
    background: #0E0E0E;
}

SidePanel ContentSwitcher {
    height: 1fr;
    background: #0E0E0E;
}

SidePanel TabPane {
    padding: 0;
    height: 1fr;
    background: #0E0E0E;
}

SidePanel Tabs {
    background: #0A0A0A;
    dock: top;
    height: 3;
    border-bottom: solid #1E1E1E;
}

SidePanel Tab {
    background: #111111;
    color: #555555;
    padding: 0 2;
    text-style: bold;
    min-width: 8;
}

SidePanel Tab:hover {
    background: #1A1A1A;
    color: #00FF41;
}

SidePanel Tab.-active {
    background: #00FF41;
    color: #0A0A0A;
    text-style: bold;
}

SidePanel Underline {
    color: #00FF41;
}

/* ── Chat log — Terminal output ──────────────────────────────── */

#chat-log {
    height: 1fr;
    background: #0A0A0A;
    color: #8A8A8A;
    scrollbar-color: #1E1E1E;
    scrollbar-color-hover: #00FF41;
    scrollbar-color-active: #FFD700;
    padding: 0 1;
}

/* ── Agent panel sections — Operative status ─────────────────── */

#agent-header {
    height: 3;
    background: #0E0E0E;
    color: #FFD700;
    text-style: bold;
    padding: 0 1;
    border-bottom: double #1E1E1E;
    content-align: left middle;
}

#agent-log {
    height: 1fr;
    background: #0A0A0A;
    color: #8A8A8A;
    scrollbar-color: #1E1E1E;
    scrollbar-color-hover: #00FF41;
    scrollbar-color-active: #FFD700;
    padding: 0 1;
}

#stats-bar {
    height: 3;
    background: #0E0E0E;
    color: #555555;
    padding: 0 1;
    border-top: solid #1E1E1E;
    content-align: left middle;
}

/* ── Input — Command line interface ──────────────────────────── */

#cmd-suggest {
    dock: bottom;
    height: auto;
    max-height: 12;
    background: #0E0E0E;
    color: #E8E8E8;
    border: double #1E1E1E;
    margin: 0 1;
    display: none;
}

#cmd-suggest:focus {
    border: double #00FF41;
}

#cmd-suggest > .option-list--option-highlighted {
    background: #00FF41 15%;
    color: #00FF41;
}

#cmd-suggest > .option-list--option {
    padding: 0 1;
}

#prompt-input {
    dock: bottom;
    height: 3;
    background: #111111;
    color: #FFD700;
    border-top: double #1E1E1E;
    padding: 0 1;
}

#prompt-input:focus {
    border-top: double #FFD700;
}

Input > .input--placeholder {
    color: #333333;
}

Input > .input--cursor {
    color: #00FF41;
    text-style: bold reverse;
}

/* ── Help overlay — Intel briefing ───────────────────────────── */

#help-overlay {
    align: center middle;
    background: rgba(0, 0, 0, 0.92);
}

#help-panel {
    width: 72;
    height: auto;
    max-height: 85%;
    background: #0E0E0E;
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
    color: #8A8A8A;
    height: auto;
    max-height: 100%;
}

/* ── Agents overlay — Roster command ─────────────────────────── */

#agents-overlay {
    align: center middle;
    background: rgba(0, 0, 0, 0.92);
}

#agents-panel {
    width: 84;
    height: auto;
    max-height: 85%;
    background: #0E0E0E;
    border: double #FFD700;
    padding: 1 2;
}

/* ── Hacker widgets ──────────────────────────────────────────── */

.hacker-header {
    height: 3;
    background: #0A0A0A;
    color: #FFD700;
    text-style: bold;
    padding: 0 1;
    border-bottom: double #1E1E1E;
    content-align: center middle;
}

.hacker-section {
    color: #00FF41;
    text-style: bold;
    padding: 1 0 0 0;
}

.hacker-border {
    border: double #1E1E1E;
    background: #0A0A0A;
}

.hacker-border:focus {
    border: double #00FF41;
}

/* Agent status bar widget */

.agent-status-bar {
    height: 3;
    background: #0A0A0A;
    padding: 0 1;
    border: solid #1E1E1E;
}

.agent-chip {
    height: 1;
    padding: 0 1;
    margin: 0 1;
}

.agent-chip-executing {
    color: #00FF41;
    text-style: bold;
}

.agent-chip-researching {
    color: #FFD700;
    text-style: italic;
}

.agent-chip-reviewing {
    color: #00BCD4;
}

.agent-chip-error {
    color: #FF1744;
    text-style: bold;
}

.agent-chip-idle {
    color: #333333;
}

/* Progress HUD */

.progress-hud {
    height: 3;
    background: #0E0E0E;
    border: solid #1E1E1E;
    padding: 0 1;
}

.progress-hud:focus {
    border: solid #00FF41;
}

/* Token burn rate sparkline */

.burn-rate {
    height: 1;
    color: #00FF41;
    background: #0A0A0A;
    padding: 0 1;
}

/* Agent dashboard grid */

.agent-dashboard {
    background: #0A0A0A;
    padding: 1;
}

.agent-card {
    height: auto;
    min-height: 6;
    width: 1fr;
    background: #0E0E0E;
    border: solid #1E1E1E;
    padding: 1;
    margin: 0 1 1 0;
}

.agent-card:hover {
    border: solid #00FF41;
}

.agent-card-name {
    color: #FFD700;
    text-style: bold;
}

.agent-card-title {
    color: #555555;
    text-style: italic;
}

.agent-card-state {
    padding: 0 1;
}

.agent-card-state-active {
    color: #00FF41;
    text-style: bold;
}

.agent-card-state-idle {
    color: #333333;
}

/* Threat panel */

.threat-row {
    height: 1;
    padding: 0 1;
}

.threat-critical {
    color: #FF1744;
    text-style: bold;
}

.threat-warning {
    color: #FF8C00;
}

.threat-info {
    color: #2196F3;
}

/* Context utilization bar */

.context-bar-container {
    height: 3;
    padding: 0 1;
    background: #0E0E0E;
}

.context-bar-fill {
    color: #00FF41;
}

.context-bar-fill-warning {
    color: #FF8C00;
}

.context-bar-fill-critical {
    color: #FF1744;
}

/* Army view grid */

.army-grid {
    background: #0A0A0A;
    padding: 1;
}

.army-cell {
    height: 3;
    width: 1fr;
    background: #0E0E0E;
    border: solid #1E1E1E;
    padding: 0 1;
    content-align: center middle;
}

.army-cell-active {
    border: solid #00FF41;
    color: #00FF41;
}

.army-cell-idle {
    color: #333333;
}

/* Matrix rain overlay */

.matrix-rain {
    background: #0A0A0A;
    color: #00FF41;
    overflow: hidden;
}

/* ── Utility classes ──────────────────────────────────────────── */

.gold {
    color: #FFD700;
}

.dim {
    color: #555555;
}

.muted {
    color: #8A8A8A;
}

.strong {
    color: #E8E8E8;
}

.link {
    color: #2196F3;
    text-style: underline;
}

.success {
    color: #00FF41;
}

.error {
    color: #FF1744;
}

.warning {
    color: #FF8C00;
}

.info {
    color: #2196F3;
}

.thinking {
    color: #00BCD4;
    text-style: italic;
}

.neon {
    color: #00FF41;
    text-style: bold;
}

.cyber {
    color: #00BCD4;
}

.threat {
    color: #FF1744;
    text-style: bold;
}

.tier-4 {
    color: #FFD700;
}

.tier-3 {
    color: #2196F3;
}

.tier-2 {
    color: #9C27B0;
}

.tier-1 {
    color: #00FF41;
}

.tool-name {
    color: #2196F3;
    text-style: bold;
}

.user-msg {
    color: #FFD700;
}

.assistant-msg {
    color: #E8E8E8;
}

.system-msg {
    color: #555555;
    text-style: italic;
}

.separator {
    color: #1E1E1E;
}

/* ── Syntax classes ───────────────────────────────────────────── */

.syn-string {
    color: #00E5CC;
}

.syn-primitive {
    color: #FFB74D;
}

.syn-property {
    color: #F06292;
}

.syn-type {
    color: #90CAF9;
}

.syn-keyword {
    color: #CE93D8;
}

.syn-comment {
    color: #555555;
}

.syn-function {
    color: #FFD700;
}

.syn-number {
    color: #FF8A80;
}
"""

__all__ = [
    "DJCODE_CSS",
    # Brand
    "GOLD",
    "DIM_GOLD",
    "DARK_GOLD",
    "ELECTRIC_GOLD",
    # Backgrounds
    "BG_PRIMARY",
    "BG_SECONDARY",
    "BG_PANEL",
    "BG_HEADER",
    "BG_INPUT",
    # Text
    "TEXT_STRONG",
    "TEXT_BASE",
    "TEXT_DIM",
    "DIM_TEXT",
    "MUTED_TEXT",
    # Borders
    "BORDER",
    "BORDER_FOCUS",
    "BORDER_GLOW",
    "BORDER_SUBTLE",
    # Interactive
    "LINK",
    # Status
    "SUCCESS",
    "ERROR",
    "WARNING",
    "INFO",
    "THINKING",
    # Modes
    "PLAN_MODE",
    "ACT_MODE",
    # Agent tiers
    "TIER_4_CONTROL",
    "TIER_3_ENTERPRISE",
    "TIER_2_ARCHITECTURE",
    "TIER_1_EXECUTION",
    # Threat agents
    "THREAT_KAVACH",
    "THREAT_VARUNA",
    "THREAT_MITRA",
    "THREAT_INDRA",
    # Syntax
    "SYN_STRING",
    "SYN_PRIMITIVE",
    "SYN_PROPERTY",
    "SYN_TYPE",
    "SYN_KEYWORD",
    "SYN_COMMENT",
    "SYN_FUNCTION",
    "SYN_NUMBER",
    # HUD
    "HUD_BORDER",
    "HUD_ACTIVE",
    "HUD_INACTIVE",
    "SCANLINE",
    "MATRIX_GREEN",
]
