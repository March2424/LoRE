"""Blue 256-color ANSI theme, Unicode-width layout helpers, and LoRE startup banner."""

import os
import re
import shutil
import sys
import unicodedata

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"

# 256-color blue palette (compatible with macOS Terminal.app, iTerm2, VS Code, and Linux).
PALETTE = {
    "bg_shade": 24,      # deep steel blue (#005f87)
    "amber_dark": 25,    # deep cobalt blue (#005faf)
    "amber_dim": 32,     # medium blue (#0087d7)
    "amber": 39,         # bright azure blue (#00afff)
    "amber_bright": 75,  # luminous sky blue (#5fafff)
    "gold": 117,         # ice blue highlight (#87d7ff)
    "coral": 209,        # coral accent (#ff875f)
    "cream": 254,        # crisp light text (#e4e4e4)
    "muted": 246,        # muted slate gray (#949494)
    "danger": 203,       # soft red (#ff5f5f)
    "ok": 81,            # cyan-blue ok (#5fd7ff)
}

GIRL_PORTRAIT = [
    "S%S*S%*%S%S%S%%*?%S%S%S%S%S%S*?%?*%%S%S%*%S%SSS%%*S%S%%S%*%%",
    "S%%?S?%%S%S%*%%**%S%S%S%S%S%S%*%%**%S%S%*%S%SSS%S*S%S%%SS*?*",
    "S%*%S*%%S%S%*%?**%%%S%S%S%S%S%#%%*#%%%S%?*S%%%S%S*S%S%%SS%**",
    "S%*%%*S%S%S*?%*,%?%%S%S%S%S%S%?%%*.*%%S%%*%%%*%%S?*%S%%SSS*?",
    "S*?%?%S%S%%*?*,..*%%S%S%S%S%%%#%?*.#?%%%S%?%%#?%S%*%S%%SSS%?",
    "S*?%*%S%S%?**,..,*?%%%S%S%S%%%#%?*,%*?%%%%%%S%*?%%*%S%%SSS%*",
    "S*%%?%S%%?#**..,.**%%%S%%%S%%*#%*,..,*?%%%%#%%%#%%?*S%%SSSS*",
    "S*%%*%%%%**,..,..,**%%S?%%S%%*#?*..,..**%%%?*%S%*??*S%%SSSS?",
    "S*%%*%%%**,..,..,..*?%%*%%S%?***..,..,..*?%%%**%%***S%%SSSS%",
    "S*%%*%%**,..,..,..,.*??#?%%%?*#*.,..,..,..%*%%?*%?*#%%%SSSS%",
    "S*%%?*%.,..,..,..,..,**#*%%%*,#.,..,..,..,..*??%?*?#%%%SSSS%",
    "S*?%%*.**#########**..,.%?%%*..,..,..*###########*?*%%%SSSS%",
    "S*?%%**####***#######*..,S**..,..,.###########**####%%*SSSS%",
    "S%*%%?%.#*SSS#@@#@#S*#*,..,*.,..,.*SS*@@S@@SS%*SS**SS%*SSSS%",
    "S%#*%%*,.*,.S#@@@@@#@*,..,..,..,..,SS#@#@@@@#@@*.,.*%*%SSSS*",
    "S%*%*%?*.,..SS#@##,.SS..,..,..,..,.SSS@@.#@.@#%.,..*%*%SSSS#",
    "S%*.*??*,..,SS*..,.*SS.,..,..,..,..SSS*..,..,*S,..*%*?%SSS%*",
    "S%?*,%?**.,.SSSS,..SSS,..:::,..,..,SSSSS,..,SSS..,*#?%%SSS%?",
    "S%?#*.*?*,..,..,..,..,..::::..,..,..,..,..,..,..,#*#?%%SS%#?",
    "S%%*?****#.,..,..,..,..,..,..,..,..,..,..,..,..,#.,*%%%SS%*?",
    "S%%*???*?**..,..,..,..,..,..,..,..,..,..,..,..,..,%*S%%S%*??",
    "S%%*?????%%.,..,..,..,..,..,..,..,..,..,..,..,..,.#%S%%S%*??",
    "S%%*??%%%%%**,..,..,..,..,####.*.,..,..,..,..,.*?**%S%%S*???",
    "S%?#?%%%%%%%%%%.,..,..,..,..,..,..,..,..,..***???*S%S%%%*???",
    "S?*?%%%%%%%%%%%%%%%..,..,..,..,..,..,..*****?????*S%S%%*????",
    "S*?*%%%%%%%%%%%%%%%%%%%%%.,..,..,..*..,..,.*?????%S%S%*?*???",
    "S*??*%%%%%*%%%%%%%%%%%%%%???**.,..,..,..,..*????*SS%S???#?*?",
    "S*??#%%%%%*%%%%%%%%%%%%%*????*,..,..,..,..,.*??*?SS%%*?##**?",
    "S*???*%%%%%%%%%?%%%%#%%%*?????..,..,..,..,..,.*#%%S%*?*?#**?",
    "S*???*%%%%%*%%%*%%%**%%%%#**??#,..,..,..,..,..,*%%*#??#*#*?*",
]

LORE_LOGO = [
    "██╗      ██████╗ ██████╗ ███████╗",
    "██║     ██╔═══██╗██╔══██╗██╔════╝",
    "██║     ██║   ██║██████╔╝█████╗  ",
    "██║     ██║   ██║██╔══██╗██╔══╝  ",
    "███████╗╚██████╔╝██║  ██║███████╗",
    "╚══════╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝",
]


def use_color() -> bool:
    """Detect whether stdout accepts ANSI escape sequences."""
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty() or os.environ.get("FORCE_COLOR") == "1"


def fg(token: str, text: str, *, bold: bool = False) -> str:
    """Wrap text in 256-color ANSI foreground escape sequence."""
    if not use_color() or not text:
        return text
    code = PALETTE[token]
    prefix = f"{BOLD}\x1b[38;5;{code}m" if bold else f"\x1b[38;5;{code}m"
    return f"{prefix}{text}{RESET}"


def strip_ansi(text: str) -> str:
    """Remove ANSI SGR sequences for width measurement."""
    return ANSI_ESCAPE.sub("", text)


def display_width(text: str) -> int:
    """Compute terminal column width accurately for ASCII + CJK text."""
    width = 0
    for ch in strip_ansi(text):
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def truncate_display(text: str, max_width: int) -> str:
    """Truncate text cleanly to max terminal columns."""
    if max_width <= 0:
        return ""
    if display_width(text) <= max_width:
        return text
    out, current = [], 0
    limit = max(0, max_width - 1)
    for ch in strip_ansi(text):
        step = 0 if unicodedata.combining(ch) else (2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1)
        if current + step > limit:
            break
        out.append(ch)
        current += step
    return "".join(out) + "…"


def wrap_ansi_display(text: str, max_width: int) -> list[str]:
    """Wrap ANSI-styled text into lines fitting within max_width terminal columns."""
    if max_width <= 0:
        return [""]
    if not text:
        return [""]
    lines: list[str] = []
    for raw_line in text.splitlines() or [""]:
        if display_width(raw_line) <= max_width:
            lines.append(raw_line)
            continue
        buf: list[str] = []
        width = 0
        active_sgr = ""
        idx = 0
        while idx < len(raw_line):
            match = ANSI_ESCAPE.match(raw_line, idx)
            if match:
                seq = match.group(0)
                buf.append(seq)
                active_sgr = "" if seq == RESET else active_sgr + seq
                idx = match.end()
                continue
            ch = raw_line[idx]
            step = 0 if unicodedata.combining(ch) else (2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1)
            if width + step > max_width and buf:
                if active_sgr:
                    buf.append(RESET)
                lines.append("".join(buf))
                buf = [active_sgr] if active_sgr else []
                width = 0
            buf.append(ch)
            width += step
            idx += 1
        if active_sgr and not (buf and buf[-1] == RESET):
            buf.append(RESET)
        lines.append("".join(buf))
    return lines or [""]


def pad_display(text: str, width: int) -> str:
    """Right-pad ANSI-styled text to an exact terminal column width."""
    gap = max(0, width - display_width(text))
    return text + (" " * gap)


def render_portrait_line(line: str, row_index: int) -> str:
    """Shade a halftone ASCII portrait row with a blue luminance ramp."""
    if not use_color():
        return line
    ramp = {
        ".": "bg_shade",
        ",": "bg_shade",
        ":": "amber_dark",
        ";": "amber_dark",
        "+": "amber_dim",
        "*": "amber_dim",
        "?": "amber",
        "%": "amber",
        "S": "amber_bright",
        "#": "gold",
        "@": "amber_dark",
    }
    chunks = []
    prev_token = None
    buf = []
    for ch in line:
        token = None if ch == " " else ramp.get(ch, "amber")
        if token != prev_token:
            if buf:
                chunk = "".join(buf)
                chunks.append(fg(prev_token, chunk) if prev_token else chunk)
            buf = [ch]
            prev_token = token
        else:
            buf.append(ch)
    if buf:
        chunk = "".join(buf)
        chunks.append(fg(prev_token, chunk) if prev_token else chunk)
    return "".join(chunks)


def terminal_panel_width(preferred: int = 104) -> int:
    """Return a safe panel width that never exceeds the current terminal columns."""
    cols = shutil.get_terminal_size((108, 30)).columns
    return min(preferred, max(36, cols - 2))


def draw_panel(lines: list[str], *, width: int, title: str = "", border: str = "amber_dim",
               wrap: bool = True) -> list[str]:
    """Render lines inside a rounded Unicode box with exact CJK padding and optional wrapping."""
    inner = max(20, width - 4)
    if title:
        title_str = truncate_display(f" {title} ", max(6, width - 4))
        rest = max(0, width - 3 - display_width(title_str))
        top = fg(border, "╭─") + fg("amber_bright", title_str, bold=True) + fg(border, "─" * rest + "╮")
    else:
        top = fg(border, "╭" + "─" * (width - 2) + "╮")
    out = [top]
    for raw in lines:
        if raw == "---":
            out.append(fg(border, "│ ") + fg("amber_dark", "─" * inner) + fg(border, " │"))
            continue
        if wrap:
            for part in wrap_ansi_display(raw, inner):
                out.append(fg(border, "│ ") + pad_display(part, inner) + fg(border, " │"))
        else:
            clipped = raw if display_width(raw) <= inner else fg("cream", truncate_display(strip_ansi(raw), inner))
            out.append(fg(border, "│ ") + pad_display(clipped, inner) + fg(border, " │"))
    out.append(fg(border, "╰" + "─" * (width - 2) + "╯"))
    return out


def draw_table(headers: list[str], rows: list[list[str]], *, max_width: int | None = None,
               wrap: bool = False) -> str:
    """Render a blue-bordered table adapted to terminal width, with optional row wrapping."""
    term_w = max_width or shutil.get_terminal_size((108, 30)).columns
    cols = len(headers)
    widths = [display_width(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            for part in (str(cell).splitlines() or [""]):
                widths[idx] = max(widths[idx], display_width(part))
    overhead = 3 * cols + 1
    budget = max(24, term_w - 2 - overhead)
    floors = [min(display_width(h), 8) for h in headers]
    while sum(widths) > budget:
        shrinkable = [i for i, w in enumerate(widths) if w > floors[i]]
        if not shrinkable:
            shrinkable = [i for i, w in enumerate(widths) if w > 4]
        if not shrinkable:
            break
        widest = max(shrinkable, key=lambda i: widths[i])
        widths[widest] -= 1

    def line(left: str, mid: str, right: str) -> str:
        return fg("amber_dark", left + mid.join("─" * (w + 2) for w in widths) + right)

    def format_cells(cells: list[str], *, header: bool = False) -> list[str]:
        wrapped = [
            wrap_ansi_display(cell, widths[idx]) if wrap
            else [truncate_display(strip_ansi(cell), widths[idx]) if display_width(cell) > widths[idx] else cell]
            for idx, cell in enumerate(cells)
        ]
        height = max(len(col) for col in wrapped)
        sep = fg("amber_dark", "│")
        rendered = []
        for row_idx in range(height):
            parts = []
            for col_idx in range(cols):
                piece = wrapped[col_idx][row_idx] if row_idx < len(wrapped[col_idx]) else ""
                styled = fg("amber_bright", piece, bold=True) if header else piece
                parts.append(" " + pad_display(styled, widths[col_idx]) + " ")
            rendered.append(sep + sep.join(parts) + sep)
        return rendered

    lines = [line("╭", "┬", "╮"), *format_cells(headers, header=True), line("├", "┼", "┤")]
    for row in rows:
        lines.extend(format_cells(row))
    lines.append(line("╰", "┴", "╯"))
    return "\n".join(lines)


def build_startup_banner(info: dict[str, str]) -> str:
    """Build the split girl-portrait + LoRE blue agent card banner (aligned 30 rows)."""
    term_width = shutil.get_terminal_size((126, 36)).columns
    card_width = max(56, min(62, term_width - 2))
    inner = max(20, card_width - 4)

    logo_colors = ["gold", "amber_bright", "amber_bright", "amber", "amber_dim", "amber_dark"]
    logo_pad = " " * max(0, (inner - display_width(LORE_LOGO[0])) // 2)
    subtitle_raw = "◈ g a m e  l o c a t i o n  a g e n t ◈"
    subtitle_pad = " " * max(0, (inner - display_width(subtitle_raw)) // 2)

    card_lines = []
    for idx, row in enumerate(LORE_LOGO):
        card_lines.append(logo_pad + fg(logo_colors[idx], row, bold=True))
    card_lines.append(
        subtitle_pad + fg("amber_dim", "◈ ") + fg("cream", "g a m e  l o c a t i o n  a g e n t") + fg("amber_dim", " ◈")
    )
    card_lines.append("")
    card_lines.append("---")
    card_lines.append("")
    card_lines.append(f"  {fg('muted', 'model    ')} {fg('amber_bright', info.get('model', 'unknown'))}")
    card_lines.append(f"  {fg('muted', 'project  ')} {fg('coral', info.get('project', 'naev'))}")
    card_lines.append(f"  {fg('muted', 'memory   ')} {fg('cream', info.get('memory', '0 rules · baseline'))}")
    card_lines.append(f"  {fg('muted', 'api      ')} {fg('amber', info.get('api', 'http://127.0.0.1:8766'))}")
    card_lines.append("")
    card_lines.append("---")
    card_lines.append("")
    card_lines.append(f"  {fg('amber_bright', 'Tools & Modules', bold=True)}")
    tools = [
        ("◎", "search_source_graph", "scoped graph & entity lookup"),
        ("◇", "import / export    ", "PO, CSV, XLSX & msgfmt check"),
        ("◆", "translate / review ", "select, check & Pi review loop"),
        ("◐", "glossary & policy  ", "P4 terms, P5 replay & memory"),
        ("✦", "api & role binding ", "per-module endpoints & probe"),
    ]
    for glyph, name, desc in tools:
        card_lines.append(f"  {fg('amber_bright', glyph)} {fg('amber', name)}  {fg('muted', desc)}")
    card_lines.append("")
    card_lines.append("---")
    card_lines.append("")
    card_lines.append(f"  {fg('muted', 'tip:')} {fg('cream', '输入自然语言下达任务，')}{fg('coral', '/api')}{fg('cream', ' 配模块，')}{fg('coral', '/help')}{fg('cream', ' 看命令')}")
    card_lines.append("")

    panel = draw_panel(card_lines, width=card_width, wrap=False)

    portrait_width = len(GIRL_PORTRAIT[0])
    if term_width < portrait_width + card_width + 2:
        art = [render_portrait_line(row, idx) for idx, row in enumerate(GIRL_PORTRAIT)]
        return "\n".join([*art, *panel]) if term_width >= portrait_width else "\n".join(panel)

    total_rows = max(len(GIRL_PORTRAIT), len(panel))
    merged = []
    for row_idx in range(total_rows):
        left = render_portrait_line(GIRL_PORTRAIT[row_idx], row_idx) if row_idx < len(GIRL_PORTRAIT) else " " * portrait_width
        right = panel[row_idx] if row_idx < len(panel) else ""
        merged.append(f"{left}  {right}")
    return "\n".join(merged)
