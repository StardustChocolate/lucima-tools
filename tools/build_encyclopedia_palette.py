"""Precompute Ark2 card colors with modern Chromium; no runtime color library.

Run with Python and Playwright's installed Chromium:
    python tools/build_encyclopedia_palette.py
    python tools/build_encyclopedia_palette.py --source E:/Project/ark2/out/角色图鉴样板/styles.css

The authored expressions are retained here after the application CSS migrates
to fixed colors. --source can read a future Ark2 stylesheet instead. No browser
network requests or project CSS edits are needed to generate the palette.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "frontend" / "encyclopedia-palette.js"

THEMES = {
    "Fire": (238, 78, 72),
    "Ice": (66, 133, 255),
    "Earth": (63, 189, 100),
    "Light": (234, 207, 67),
    "Dark": (183, 92, 229),
}
STATUSES = {
    "buff": (79, 211, 164),
    "debuff": (244, 104, 115),
    "neutral": (190, 146, 76),
}

# Snapshot of the 44 distinct authored expressions in encyclopedia-card.css.
# Keep the formulas so regeneration works after the CSS uses the fixed palette.
EXPRESSIONS = (
    "color-mix(in oklch, var(--theme-color) 90%, black)",
    "color-mix(in oklch, var(--theme-color) 58%, #8d999b)",
    "color-mix(in oklch, var(--theme-color) 82%, #a9b4b6)",
    "color-mix(in oklch, var(--theme-color) 88%, white)",
    "color-mix(in oklch, var(--theme-color) 58%, transparent)",
    "color-mix(in oklch, var(--theme-color) 68%, white)",
    "color-mix(in oklch, var(--theme-color) 22%, white)",
    "color-mix(in oklch, #080c10 94%, var(--theme-color))",
    "color-mix(in oklch, var(--theme-color) 42%, transparent)",
    "color-mix(in oklch, var(--theme-color) 78%, white)",
    "color-mix(in oklch, var(--theme-color) 62%, transparent)",
    "color-mix(in oklch, var(--theme-color) 82%, transparent)",
    "color-mix(in oklch, var(--theme-color) 48%, transparent)",
    "color-mix(in oklch, var(--theme-color) 72%, transparent)",
    "color-mix(in oklch, var(--theme-color) 32%, transparent)",
    "color-mix(in oklch, var(--theme-color) 28%, transparent)",
    "color-mix(in oklch, var(--theme-color) 30%, transparent)",
    "color-mix(in oklch, var(--theme-color) 38%, white)",
    "color-mix(in oklch, var(--theme-color) 22%, transparent)",
    "color-mix(in oklch, var(--theme-color) 70%, transparent)",
    "color-mix(in oklch, var(--theme-color) 68%, transparent)",
    "color-mix(in oklch, var(--theme-color) 26%, white)",
    "color-mix(in oklch, var(--theme-color) 38%, transparent)",
    "color-mix(in oklch, var(--theme-color) 40%, white)",
    "color-mix(in oklch, var(--status-color) 55%, white)",
    "color-mix(in oklch, var(--status-color) 62%, white)",
    "color-mix(in oklch, var(--status-color) 32%, transparent)",
    "color-mix(in oklch, var(--status-color) 60%, transparent)",
    "color-mix(in oklch, var(--status-color) 18%, transparent)",
    "color-mix(in oklch, var(--status-color) 8%, transparent)",
    "color-mix(in oklch, var(--status-color) 10%, transparent)",
    "color-mix(in oklch, var(--status-color) 38%, transparent)",
    "color-mix(in oklch, var(--status-color) 40%, white)",
    "color-mix(in oklch, var(--theme-color) 25%, #d9e0e2)",
    "color-mix(in oklch, var(--theme-color) 16%, transparent)",
    "color-mix(in oklch, var(--theme-color) 55%, white)",
    "color-mix(in oklch, var(--theme-color) 76%, transparent)",
    "color-mix(in oklch, var(--theme-color) 52%, transparent)",
    "color-mix(in oklch, var(--theme-color) 46%, white)",
    "color-mix(in oklch, var(--theme-color) 34%, transparent)",
    "color-mix(in oklch, var(--theme-color) 24%, transparent)",
    "color-mix(in oklch, var(--theme-color) 55%, transparent)",
    "color-mix(in oklch, var(--theme-color) 30%, #dce5e7)",
    "color-mix(in oklch, var(--theme-color) 52%, white)",
)

EXPRESSION_PATTERN = re.compile(r"color-mix\(in oklch,(?:[^()]|\([^()]*\))+\)")
ORDINARY_PATTERN = re.compile(
    r"color-mix\(in oklch, var\(--(theme|status)-color\) (\d+)%, (white|black|transparent|#[0-9a-f]{6})\)"
)
SURFACE_PATTERN = re.compile(
    r"color-mix\(in oklch, (#[0-9a-f]{6}) (\d+)%, var\(--theme-color\)\)"
)


def expression_key(expression: str) -> tuple[str, str]:
    ordinary = ORDINARY_PATTERN.fullmatch(expression)
    if ordinary:
        scope, percent, other = ordinary.groups()
        other_key = f"gray-{other[1:]}" if other.startswith("#") else other
        return scope, f"--{scope}-{other_key}-{percent}"
    surface = SURFACE_PATTERN.fullmatch(expression)
    if surface and surface.groups() == ("#080c10", "94"):
        return "theme", "--theme-surface"
    raise ValueError(f"Unsupported card color expression: {expression}")


def read_expressions(source: Path | None) -> tuple[str, ...]:
    if source is None:
        return EXPRESSIONS
    text = source.read_text(encoding="utf-8")
    expressions = tuple(dict.fromkeys(EXPRESSION_PATTERN.findall(text)))
    if not expressions:
        raise ValueError(f"No authored color-mix expressions found in {source}")
    for expression in expressions:
        expression_key(expression)
    return expressions


VERIFY_COLOR_ENGINE = """version => {
    // Older Chromium accepts this syntax but handles powerless OKLCH hues
    // differently. Verify actual pixels before trusting its color conversion.
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 1;
    const context = canvas.getContext('2d', {willReadFrequently: true});
    const samples = [
        {name: 'Fire surface', expression: 'color-mix(in oklch, #080c10 94%, rgb(238, 78, 72))', expected: [27, 14, 13, 255]},
        {name: 'buff text', expression: 'color-mix(in oklch, rgb(79, 211, 164) 40%, white)', expected: [193, 238, 218, 255]},
    ];
    try {
        const failures = [];
        for (const sample of samples) {
            context.clearRect(0, 0, 1, 1);
            context.fillStyle = '#000';
            context.fillStyle = sample.expression;
            context.fillRect(0, 0, 1, 1);
            const actual = [...context.getImageData(0, 0, 1, 1).data];
            if (actual.some((channel, index) => Math.abs(channel - sample.expected[index]) > 1)) {
                failures.push(`${sample.name}: got [${actual}], expected [${sample.expected}]`);
            }
        }
        if (failures.length) {
            throw new Error(`Chromium ${version} has incompatible OKLCH rendering. Use modern Chromium; palette generation refused. ${failures.join('; ')}`);
        }
    } finally {
        canvas.width = canvas.height = 0;
    }
}"""


COMPUTE_COLORS = """records => {
    if (!CSS.supports('color', 'color(from red srgb r g b / alpha)')) {
        throw new Error('Palette generation requires a modern Chromium with relative colors');
    }
    const probe = document.createElement('i');
    document.body.append(probe);
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 1;
    const context = canvas.getContext('2d', {willReadFrequently: true});
    const format = number => String(Number(number.toFixed(6)));
    function paint(color, background) {
        context.clearRect(0, 0, 1, 1);
        context.fillStyle = background;
        context.fillRect(0, 0, 1, 1);
        context.fillStyle = color;
        context.fillRect(0, 0, 1, 1);
        return [...context.getImageData(0, 0, 1, 1).data];
    }
    try {
        return records.map(record => {
            // Relative-color conversion preserves the formula's full alpha.
            // Canvas bytes are only used for verification, never for deriving
            // alpha (which would change .42 to 107/255).
            probe.style.color = `color(from ${record.expression} srgb r g b / alpha)`;
            const computed = getComputedStyle(probe).color;
            const match = computed.match(/^color\\(srgb ([^/()]+)(?:\\/ ([^()]+))?\\)$/);
            if (!match) throw new Error(`Unexpected computed color: ${computed}`);
            const channels = match[1].trim().split(/\\s+/).map(Number);
            const alpha = match[2] === undefined ? 1 : Number(match[2]);
            if (channels.length !== 3 || !channels.every(Number.isFinite) || !Number.isFinite(alpha)) {
                throw new Error(`Invalid computed color: ${computed}`);
            }
            const rgb = channels.map(channel => Math.max(0, Math.min(255, channel * 255)));
            const value = alpha === 1
                ? `rgb(${rgb.map(format).join(', ')})`
                : `rgba(${rgb.map(format).join(', ')}, ${format(alpha)})`;
            let maxDelta = 0;
            for (const background of ['white', 'black', 'rgb(27,14,13)', 'rgb(12,18,28)']) {
                const original = paint(record.expression, background);
                const fixed = paint(value, background);
                maxDelta = Math.max(maxDelta, ...original.map((channel, index) => Math.abs(channel - fixed[index])));
            }
            // Validate the visible composite on opaque surfaces. Reading an
            // 8-bit transparent canvas unpremultiplies quantized alpha, which
            // can amplify a harmless rounding difference by several RGB units.
            if (maxDelta > 1) throw new Error(`Color conversion changed pixels by ${maxDelta}: ${record.expression} -> ${value}`);
            return {...record, value, maxDelta};
        });
    } finally {
        probe.remove();
        canvas.width = canvas.height = 0;
    }
}"""


def generate_palette(expressions: tuple[str, ...], executable: Path | None = None) -> dict:
    palette = {"themes": {}, "statuses": {}}
    records = []
    for category, groups, scope in (("themes", THEMES, "theme"), ("statuses", STATUSES, "status")):
        for name, channels in groups.items():
            values = {f"--{scope}-rgb" if scope == "theme" else f"--{name}-rgb": ", ".join(map(str, channels))}
            palette[category][name] = values
            for expression in expressions:
                expression_scope, key = expression_key(expression)
                if expression_scope != scope:
                    continue
                if scope == "status":
                    key = key.replace("--status-", f"--{name}-", 1)
                records.append({
                    "category": category, "name": name, "key": key,
                    "expression": expression.replace(f"var(--{scope}-color)", f"rgb({', '.join(map(str, channels))})"),
                })
    with sync_playwright() as playwright:
        options = {"headless": True}
        if executable:
            options["executable_path"] = str(executable)
        browser = playwright.chromium.launch(**options)
        try:
            page = browser.new_page()
            # The generation page never needs a network resource.
            page.route("**/*", lambda route: route.abort())
            page.evaluate(VERIFY_COLOR_ENGINE, browser.version)
            computed = page.evaluate(COMPUTE_COLORS, records)
            print(f"Computed {len(expressions)} formulas / {len(computed)} colors using {browser.version}")
            print(f"Maximum verified 8-bit channel delta: {max(record['maxDelta'] for record in computed)}")
            for record in computed:
                palette[record["category"]][record["name"]][record["key"]] = record["value"]
        finally:
            browser.close()
    return palette


def write_palette(path: Path, palette: dict) -> None:
    payload = json.dumps(palette, indent=2, ensure_ascii=True)
    path.write_text(
        '"use strict";\n\n'
        '// Generated by tools/build_encyclopedia_palette.py from the authored Ark2\n'
        '// OKLCH formulas, converted to sRGB by modern Chromium. Decimal RGB and\n'
        '// original alpha are retained; no runtime color-mix support is required.\n'
        f'window.LucimaEncyclopediaPalette = Object.freeze({payload});\n',
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Optional original Ark2 card CSS; defaults to the retained formulas")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--chromium", type=Path, help="Optional modern Chromium executable")
    args = parser.parse_args()
    palette = generate_palette(read_expressions(args.source), args.chromium)
    write_palette(args.output, palette)
    print(f"Wrote {args.output} ({args.output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
