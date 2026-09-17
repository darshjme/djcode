"""Shared terminal styling derived from cli.darshj.ai’s workspace."""

from __future__ import annotations

# ── Color palette ─────────────────────────────────────────────────────────

# Brand / Primary
ACCENT = "#7C96FF"
GOLD = ACCENT  # Compatibility alias for existing widgets.
DIM_GOLD = "#B8960F"
DARK_GOLD = "#3D2E00"
ELECTRIC_GOLD = "#7C96FF"

# Backgrounds (deep black layered depth)
BG_PRIMARY = "#080808"
BG_SECONDARY = "#111112"
BG_PANEL = "#161617"
BG_HEADER = "#080808"
BG_INPUT = "#1E1E21"

# Text hierarchy
TEXT_STRONG = "#F4F4F4"
TEXT_BASE = "#D4D4D8"
TEXT_DIM = "#A0A0A5"
DIM_TEXT = TEXT_DIM  # compat alias
MUTED_TEXT = TEXT_BASE  # compat alias

# Borders
BORDER = "#29292C"
BORDER_FOCUS = "#7C96FF"
BORDER_GLOW = "#7C96FF"
BORDER_SUBTLE = "#1A1A1A"

# Interactive
LINK = "#7C96FF"

# Status — vibrant cyberpunk palette
SUCCESS = "#A2BA9A"       # Matrix neon green
ERROR = "#ED9393"         # Blood red
WARNING = "#D4B483"       # Amber
INFO = "#7C96FF"          # Electric blue
THINKING = "#9AAFF0"      # Cyan/teal for AI thinking

# Modes
PLAN_MODE = "#B7A1D9"     # Purple — architecture mode
ACT_MODE = "#A2BA9A"      # Neon green — execution mode

# Agent tier accents
TIER_4_CONTROL = "#7C96FF"   # Pure gold — Vyasa, Control tier
TIER_3_ENTERPRISE = "#7C96FF"  # Electric blue — Enterprise tier
TIER_2_ARCHITECTURE = "#B7A1D9"  # Purple — Architecture tier
TIER_1_EXECUTION = "#A2BA9A"   # Neon green — Execution tier

# Threat agents
THREAT_KAVACH = "#ED9393"
THREAT_VARUNA = "#FF6D00"
THREAT_MITRA = "#D4B483"
THREAT_INDRA = "#D50000"

# Syntax highlighting (tuned for dark bg)
SYN_STRING = "#00E5CC"
SYN_PRIMITIVE = "#FFB74D"
SYN_PROPERTY = "#F06292"
SYN_TYPE = "#90CAF9"
SYN_KEYWORD = "#CE93D8"
SYN_COMMENT = "#A0A0A5"
SYN_FUNCTION = "#7C96FF"
SYN_NUMBER = "#FF8A80"

# HUD elements
HUD_BORDER = "#333333"
HUD_ACTIVE = "#A2BA9A"
HUD_INACTIVE = "#1A1A1A"
SCANLINE = "rgba(0, 255, 65, 0.03)"
MATRIX_GREEN = "#A2BA9A"

# ── Main CSS ──────────────────────────────────────────────────────────────

DJCODE_CSS = """
#hacker-header { height: 1; border: none; background: #080808; }
#agent-status-bar { height: 1; border: none; background: #080808; }
#workflow-state { height: 1; padding: 0 2; color: #A2BA9A; background: #080808; }


/* ================================================================
   DJcode workspace — charcoal surfaces, quiet borders and cobalt focus
   ================================================================ */

/* ── Screen ───────────────────────────────────────────────────── */

Screen {
    background: #080808;
    color: #D4D4D8;
}

/* Preserve the application beneath a tool permission dialog. */
ToolApprovalScreen {
    background: rgba(0, 0, 0, 0.60);
}

/* Fit all dialogs inside small terminals without hiding their controls. */
#approval-box, #help-box, #agents-box, #model-box, #provider-box, #palette-box, #search-box {
    max-width: 95%;
    max-height: 90%;
}
#status-bar { color: #A1A1AA; border-top: none; }

/* ── Header — Military HUD bar ────────────────────────────────── */

Header {
    background: #080808;
    color: #7C96FF;
    dock: top;
    height: 1;
    border-bottom: solid #29292C;
}

HeaderTitle {
    color: #7C96FF;
    text-style: bold;
}

/* ── Footer — Status telemetry strip ─────────────────────────── */

Footer {
    background: #080808;
    color: #A0A0A5;
    dock: bottom;
    height: 1;
    border: none;
}

FooterKey {
    background: #1E1E21;
    color: #A2BA9A;
}

FooterKey:hover {
    background: #1A1A1A;
    color: #7C96FF;
}

/* ── Status bar — system telemetry ───────────────────────────── */

#status-bar {
    dock: none;
    height: 1;
    background: #161617;
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
    border-title-color: #7C96FF;
    border-title-style: bold;
    background: #080808;
}

#chat-panel:focus-within {
    border: none;
}

/* ── Side panel (35%) — Intelligence dashboard ───────────────── */

#side-panel {
    width: 35%;
    border: solid #29292C;
    border-title-color: #A2BA9A;
    border-title-style: bold;
    background: #161617;
    padding: 0;
}

#side-panel:focus-within {
    border: solid #A2BA9A;
}

/* ── SidePanel tabs — HUD navigation ─────────────────────────── */

SidePanel TabbedContent {
    height: 100%;
    background: #161617;
}

SidePanel ContentSwitcher {
    height: 1fr;
    background: #161617;
}

SidePanel TabPane {
    padding: 0;
    height: 1fr;
    background: #161617;
}

SidePanel Tabs {
    background: #080808;
    dock: top;
    height: 3;
    border-bottom: solid #29292C;
}

SidePanel Tab {
    background: #1E1E21;
    color: #A0A0A5;
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
    color: #080808;
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
    background: #080808;
    color: #D4D4D8;
    scrollbar-color: #29292C;
    scrollbar-color-hover: #A2BA9A;
    scrollbar-color-active: #7C96FF;
    padding: 1 2;
}

/* ── Agent panel sections — Operative status ─────────────────── */

#agent-header {
    height: 3;
    background: #161617;
    color: #7C96FF;
    text-style: bold;
    padding: 0 1;
    border-bottom: double #29292C;
    content-align: left middle;
}

#agent-log {
    height: 1fr;
    background: #080808;
    color: #D4D4D8;
    scrollbar-color: #29292C;
    scrollbar-color-hover: #A2BA9A;
    scrollbar-color-active: #7C96FF;
    padding: 0 1;
}

#stats-bar {
    height: 3;
    background: #161617;
    color: #A0A0A5;
    padding: 0 1;
    border-top: solid #29292C;
    content-align: left middle;
}

/* ── Input — Command line interface ──────────────────────────── */

#cmd-suggest {
    dock: none;
    height: auto;
    max-height: 7;
    background: #161617;
    color: #F4F4F4;
    border: solid #29292C;
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
    background: #1E1E21;
    color: #7C96FF;
    border: round #55514A;
    margin: 0 1;
    padding: 0 1;
}

#prompt-input:focus {
    border: round #7C96FF;
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
    background: #161617;
    border: solid #7C96FF;
    padding: 1 2;
}

#help-title {
    text-style: bold;
    color: #7C96FF;
    text-align: center;
    margin-bottom: 1;
}

#help-content {
    color: #D4D4D8;
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
    background: #161617;
    border: solid #7C96FF;
    padding: 1 2;
}

/* ── Hacker widgets ──────────────────────────────────────────── */

.hacker-header {
    height: 3;
    background: #080808;
    color: #7C96FF;
    text-style: bold;
    padding: 0 1;
    border-bottom: double #29292C;
    content-align: center middle;
}

.hacker-section {
    color: #A2BA9A;
    text-style: bold;
    padding: 1 0 0 0;
}

.hacker-border {
    border: solid #29292C;
    background: #080808;
}

.hacker-border:focus {
    border: solid #A2BA9A;
}

/* Agent status bar widget */

.agent-status-bar {
    height: 3;
    background: #080808;
    padding: 0 1;
    border: solid #29292C;
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
    color: #7C96FF;
    text-style: italic;
}

.agent-chip-reviewing {
    color: #9AAFF0;
}

.agent-chip-error {
    color: #ED9393;
    text-style: bold;
}

.agent-chip-idle {
    color: #333333;
}

/* Progress HUD */

.progress-hud {
    height: 3;
    background: #161617;
    border: solid #29292C;
    padding: 0 1;
}

.progress-hud:focus {
    border: solid #A2BA9A;
}

/* Token burn rate sparkline */

.burn-rate {
    height: 1;
    color: #A2BA9A;
    background: #080808;
    padding: 0 1;
}

/* Agent dashboard grid */

.agent-dashboard {
    background: #080808;
    padding: 1;
}

.agent-card {
    height: auto;
    min-height: 6;
    width: 1fr;
    background: #161617;
    border: solid #29292C;
    padding: 1;
    margin: 0 1 1 0;
}

.agent-card:hover {
    border: solid #A2BA9A;
}

.agent-card-name {
    color: #7C96FF;
    text-style: bold;
}

.agent-card-title {
    color: #A0A0A5;
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
    color: #ED9393;
    text-style: bold;
}

.threat-warning {
    color: #D4B483;
}

.threat-info {
    color: #7C96FF;
}

/* Context utilization bar */

.context-bar-container {
    height: 3;
    padding: 0 1;
    background: #161617;
}

.context-bar-fill {
    color: #A2BA9A;
}

.context-bar-fill-warning {
    color: #D4B483;
}

.context-bar-fill-critical {
    color: #ED9393;
}

/* Army view grid */

.army-grid {
    background: #080808;
    padding: 1;
}

.army-cell {
    height: 3;
    width: 1fr;
    background: #161617;
    border: solid #29292C;
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
    background: #080808;
    color: #A2BA9A;
    overflow: hidden;
}

/* ── Utility classes ──────────────────────────────────────────── */

.gold {
    color: #7C96FF;
}

.dim {
    color: #A0A0A5;
}

.muted {
    color: #D4D4D8;
}

.strong {
    color: #F4F4F4;
}

.link {
    color: #7C96FF;
    text-style: underline;
}

.success {
    color: #A2BA9A;
}

.error {
    color: #ED9393;
}

.warning {
    color: #D4B483;
}

.info {
    color: #7C96FF;
}

.thinking {
    color: #9AAFF0;
    text-style: italic;
}

.neon {
    color: #A2BA9A;
    text-style: bold;
}

.cyber {
    color: #9AAFF0;
}

.threat {
    color: #ED9393;
    text-style: bold;
}

.tier-4 {
    color: #7C96FF;
}

.tier-3 {
    color: #7C96FF;
}

.tier-2 {
    color: #B7A1D9;
}

.tier-1 {
    color: #A2BA9A;
}

.tool-name {
    color: #7C96FF;
    text-style: bold;
}

.user-msg {
    color: #7C96FF;
}

.assistant-msg {
    color: #F4F4F4;
}

.system-msg {
    color: #A0A0A5;
    text-style: italic;
}

.separator {
    color: #29292C;
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
    color: #A0A0A5;
}

.syn-function {
    color: #7C96FF;
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

# Workspace chrome stays compact; narrow terminals retain command access via F4.
DJCODE_CSS += """
#workspace-nav {
    width: 25;
    height: 1fr;
    padding: 1;
    background: #111112;
    border-right: solid #29292C;
    overflow-y: auto;
}
#workspace-nav .nav-heading { color: #A0A0A5; height: 2; padding-top: 1; }
#workspace-nav Button {
    width: 100%; min-width: 0; height: 1; min-height: 1;
    border: none; background: transparent; color: #A0A0A5;
    text-align: left; content-align: left middle; text-style: none; padding: 0 1; margin-bottom: 1;
}
#workspace-nav Button:hover, #workspace-nav Button:focus {
    background: #242427; color: #F4F4F4;
}
#workspace-nav #nav-build { background: #242427; color: #F4F4F4; }
#workspace-nav #nav-scout { color: #B7A1D9; }
#workspace-nav #nav-architect { color: #7C96FF; }
#workspace-nav #nav-build-agent { color: #D4B483; }
#workspace-nav #nav-test { color: #A2BA9A; }
#workspace-nav .nav-footnote { margin-top: 1; color: #A0A0A5; height: auto; }
#workspace-title { height: 3; padding: 0 2; content-align: left middle; color: #A0A0A5; border-bottom: solid #29292C; background: #111112; }
#prompt-input { border: round #29292C; background: #1E1E21; }
#prompt-input:focus { border: round #7C96FF; }
#chat-panel { width: 1fr; }
"""
