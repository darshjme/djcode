"""Conversation-first terminal styling with a warm accent and quiet status controls."""

from __future__ import annotations

# ── Color palette ─────────────────────────────────────────────────────────

# Brand / Primary
GOLD = "#C79B7A"
DIM_GOLD = "#B8960F"
DARK_GOLD = "#3D2E00"
ELECTRIC_GOLD = "#C79B7A"

# Backgrounds (deep black layered depth)
BG_PRIMARY = "#17191D"
BG_SECONDARY = "#121212"
BG_PANEL = "#1B1E23"
BG_HEADER = "#17191D"
BG_INPUT = "#202329"

# Text hierarchy
TEXT_STRONG = "#E8E8E8"
TEXT_BASE = "#C2BFB8"
TEXT_DIM = "#96928C"
DIM_TEXT = TEXT_DIM  # compat alias
MUTED_TEXT = TEXT_BASE  # compat alias

# Borders
BORDER = "#1E1E1E"
BORDER_FOCUS = "#C79B7A"
BORDER_GLOW = "#C79B7A"
BORDER_SUBTLE = "#1A1A1A"

# Interactive
LINK = "#2196F3"

# Status — vibrant cyberpunk palette
SUCCESS = "#A2BA9A"       # Matrix neon green
ERROR = "#FF1744"         # Blood red
WARNING = "#FF8C00"       # Amber
INFO = "#2196F3"          # Electric blue
THINKING = "#00BCD4"      # Cyan/teal for AI thinking

# Modes
PLAN_MODE = "#9C27B0"     # Purple — architecture mode
ACT_MODE = "#A2BA9A"      # Neon green — execution mode

# Agent tier accents
TIER_4_CONTROL = "#C79B7A"   # Pure gold — Vyasa, Control tier
TIER_3_ENTERPRISE = "#2196F3"  # Electric blue — Enterprise tier
TIER_2_ARCHITECTURE = "#9C27B0"  # Purple — Architecture tier
TIER_1_EXECUTION = "#A2BA9A"   # Neon green — Execution tier

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
SYN_COMMENT = "#96928C"
SYN_FUNCTION = "#C79B7A"
SYN_NUMBER = "#FF8A80"

# HUD elements
HUD_BORDER = "#333333"
HUD_ACTIVE = "#A2BA9A"
HUD_INACTIVE = "#1A1A1A"
SCANLINE = "rgba(0, 255, 65, 0.03)"
MATRIX_GREEN = "#A2BA9A"

# ── Main CSS ──────────────────────────────────────────────────────────────

DJCODE_CSS = """
#hacker-header { height: 1; border: none; background: #17191D; }
#agent-status-bar { height: 1; border: none; background: #17191D; }
#workflow-state { height: 1; padding: 0 2; color: #A2BA9A; background: #17191D; }


/* ================================================================
   DJcode v4.0 — HACKER COMMAND CENTER THEME
   Cyberpunk terminal | Matrix aesthetic | Military HUD
   ================================================================ */

/* ── Screen ───────────────────────────────────────────────────── */

Screen {
    background: #17191D;
    color: #C2BFB8;
}

/* Preserve the application beneath a tool permission dialog. */
ToolApprovalScreen {
    background: rgba(0, 0, 0, 0.60);
}

/* Fit all dialogs inside small terminals without hiding their controls. */
#approval-box, #help-box, #agents-box, #model-box, #provider-box, #palette-box {
    max-width: 95%;
    max-height: 90%;
}
#status-bar { color: #A1A1AA; border-top: none; }

/* ── Header — Military HUD bar ────────────────────────────────── */

Header {
    background: #17191D;
    color: #C79B7A;
    dock: top;
    height: 1;
    border-bottom: solid #1E1E1E;
}

HeaderTitle {
    color: #C79B7A;
    text-style: bold;
}

/* ── Footer — Status telemetry strip ─────────────────────────── */

Footer {
    background: #17191D;
    color: #96928C;
    dock: bottom;
    height: 1;
    border: none;
}

FooterKey {
    background: #202329;
    color: #A2BA9A;
}

FooterKey:hover {
    background: #1A1A1A;
    color: #C79B7A;
}

/* ── Status bar — system telemetry ───────────────────────────── */

#status-bar {
    dock: none;
    height: 1;
    background: #1B1E23;
    color: #A1A1AA;
    padding: 0 1;
    border-top: none;
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
    border: none;
    border-title-color: #C79B7A;
    border-title-style: bold;
    background: #17191D;
}

#chat-panel:focus-within {
    border: none;
}

/* ── Side panel (35%) — Intelligence dashboard ───────────────── */

#side-panel {
    width: 35%;
    border: solid #1E1E1E;
    border-title-color: #A2BA9A;
    border-title-style: bold;
    background: #1B1E23;
    padding: 0;
}

#side-panel:focus-within {
    border: solid #A2BA9A;
}

/* ── SidePanel tabs — HUD navigation ─────────────────────────── */

SidePanel TabbedContent {
    height: 100%;
    background: #1B1E23;
}

SidePanel ContentSwitcher {
    height: 1fr;
    background: #1B1E23;
}

SidePanel TabPane {
    padding: 0;
    height: 1fr;
    background: #1B1E23;
}

SidePanel Tabs {
    background: #17191D;
    dock: top;
    height: 3;
    border-bottom: solid #1E1E1E;
}

SidePanel Tab {
    background: #202329;
    color: #96928C;
    padding: 0 2;
    text-style: bold;
    min-width: 8;
}

SidePanel Tab:hover {
    background: #1A1A1A;
    color: #A2BA9A;
}

SidePanel Tab.-active {
    background: #A2BA9A;
    color: #17191D;
    text-style: bold;
}

SidePanel Underline {
    color: #A2BA9A;
}

/* ── Chat log — Terminal output ──────────────────────────────── */

#chat-log {
    height: 1fr;
    overflow-y: scroll;
    overflow-x: hidden;
    background: #17191D;
    color: #C2BFB8;
    scrollbar-color: #1E1E1E;
    scrollbar-color-hover: #A2BA9A;
    scrollbar-color-active: #C79B7A;
    padding: 1 2;
}

/* ── Agent panel sections — Operative status ─────────────────── */

#agent-header {
    height: 3;
    background: #1B1E23;
    color: #C79B7A;
    text-style: bold;
    padding: 0 1;
    border-bottom: double #1E1E1E;
    content-align: left middle;
}

#agent-log {
    height: 1fr;
    background: #17191D;
    color: #C2BFB8;
    scrollbar-color: #1E1E1E;
    scrollbar-color-hover: #A2BA9A;
    scrollbar-color-active: #C79B7A;
    padding: 0 1;
}

#stats-bar {
    height: 3;
    background: #1B1E23;
    color: #96928C;
    padding: 0 1;
    border-top: solid #1E1E1E;
    content-align: left middle;
}

/* ── Input — Command line interface ──────────────────────────── */

#cmd-suggest {
    dock: none;
    height: auto;
    max-height: 7;
    background: #1B1E23;
    color: #E8E8E8;
    border: solid #1E1E1E;
    margin: 0 1;
    display: none;
}

#cmd-suggest:focus {
    border: solid #A2BA9A;
}

#cmd-suggest > .option-list--option-highlighted {
    background: #A2BA9A 15%;
    color: #A2BA9A;
}

#cmd-suggest > .option-list--option {
    padding: 0 1;
}

#prompt-input {
    dock: none;
    height: 3;
    background: #202329;
    color: #C79B7A;
    border: round #55514A;
    margin: 0 1;
    padding: 0 1;
}

#prompt-input:focus {
    border: round #C79B7A;
}

Input > .input--placeholder {
    color: #85858D;
}

Input > .input--cursor {
    color: #A2BA9A;
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
    background: #1B1E23;
    border: solid #C79B7A;
    padding: 1 2;
}

#help-title {
    text-style: bold;
    color: #C79B7A;
    text-align: center;
    margin-bottom: 1;
}

#help-content {
    color: #C2BFB8;
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
    background: #1B1E23;
    border: solid #C79B7A;
    padding: 1 2;
}

/* ── Hacker widgets ──────────────────────────────────────────── */

.hacker-header {
    height: 3;
    background: #17191D;
    color: #C79B7A;
    text-style: bold;
    padding: 0 1;
    border-bottom: double #1E1E1E;
    content-align: center middle;
}

.hacker-section {
    color: #A2BA9A;
    text-style: bold;
    padding: 1 0 0 0;
}

.hacker-border {
    border: solid #1E1E1E;
    background: #17191D;
}

.hacker-border:focus {
    border: solid #A2BA9A;
}

/* Agent status bar widget */

.agent-status-bar {
    height: 3;
    background: #17191D;
    padding: 0 1;
    border: solid #1E1E1E;
}

.agent-chip {
    height: 1;
    padding: 0 1;
    margin: 0 1;
}

.agent-chip-executing {
    color: #A2BA9A;
    text-style: bold;
}

.agent-chip-researching {
    color: #C79B7A;
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
    background: #1B1E23;
    border: solid #1E1E1E;
    padding: 0 1;
}

.progress-hud:focus {
    border: solid #A2BA9A;
}

/* Token burn rate sparkline */

.burn-rate {
    height: 1;
    color: #A2BA9A;
    background: #17191D;
    padding: 0 1;
}

/* Agent dashboard grid */

.agent-dashboard {
    background: #17191D;
    padding: 1;
}

.agent-card {
    height: auto;
    min-height: 6;
    width: 1fr;
    background: #1B1E23;
    border: solid #1E1E1E;
    padding: 1;
    margin: 0 1 1 0;
}

.agent-card:hover {
    border: solid #A2BA9A;
}

.agent-card-name {
    color: #C79B7A;
    text-style: bold;
}

.agent-card-title {
    color: #96928C;
    text-style: italic;
}

.agent-card-state {
    padding: 0 1;
}

.agent-card-state-active {
    color: #A2BA9A;
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
    background: #1B1E23;
}

.context-bar-fill {
    color: #A2BA9A;
}

.context-bar-fill-warning {
    color: #FF8C00;
}

.context-bar-fill-critical {
    color: #FF1744;
}

/* Army view grid */

.army-grid {
    background: #17191D;
    padding: 1;
}

.army-cell {
    height: 3;
    width: 1fr;
    background: #1B1E23;
    border: solid #1E1E1E;
    padding: 0 1;
    content-align: center middle;
}

.army-cell-active {
    border: solid #A2BA9A;
    color: #A2BA9A;
}

.army-cell-idle {
    color: #333333;
}

/* Matrix rain overlay */

.matrix-rain {
    background: #17191D;
    color: #A2BA9A;
    overflow: hidden;
}

/* ── Utility classes ──────────────────────────────────────────── */

.gold {
    color: #C79B7A;
}

.dim {
    color: #96928C;
}

.muted {
    color: #C2BFB8;
}

.strong {
    color: #E8E8E8;
}

.link {
    color: #2196F3;
    text-style: underline;
}

.success {
    color: #A2BA9A;
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
    color: #A2BA9A;
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
    color: #C79B7A;
}

.tier-3 {
    color: #2196F3;
}

.tier-2 {
    color: #9C27B0;
}

.tier-1 {
    color: #A2BA9A;
}

.tool-name {
    color: #2196F3;
    text-style: bold;
}

.user-msg {
    color: #C79B7A;
}

.assistant-msg {
    color: #E8E8E8;
}

.system-msg {
    color: #96928C;
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
    color: #96928C;
}

.syn-function {
    color: #C79B7A;
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
