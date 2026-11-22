# epub-normalize-fonts

Makes EPUB books follow your e-reader's own font, size and line spacing settings, and gives
them consistent paragraph formatting. It removes embedded fonts and the publisher's font
choices, brings the main text to your reader's default size, and formats paragraphs the way
printed books do. Single file, Python standard library only.

## Install

```bash
pipx install .        # or: pip install .
```

Or run it directly: `python3 epub_normalize_fonts.py book.epub`.

## Usage

```bash
epub-normalize-fonts book.epub              # writes book-normalized.epub next to it
epub-normalize-fonts -o clean/ ~/Books      # every .epub in ~/Books -> clean/
epub-normalize-fonts -i *.epub              # modify in place
epub-normalize-fonts -n -v -r ~/Books       # dry run, recursive, show details
```

| Option | Meaning |
| --- | --- |
| `-i, --in-place` | overwrite the input files (written atomically via a temp file) |
| `-o, --output-dir DIR` | write normalized books into `DIR` with the same names |
| `-s, --suffix` | suffix for output written next to the input (default `-normalized`) |
| `-r, --recursive` | search directories recursively |
| `-n, --dry-run` | report only, write nothing |
| `-f, --force` | overwrite existing output files |
| `-v` / `-q` | list removed font files and body text size / only print errors |

Running it again on a normalized book changes nothing.

## What it changes

**Embedded fonts**
- Deletes font files (`.ttf .otf .woff .woff2 .eot .ttc …`, plus anything the OPF manifest
  declares as a font) and removes them from the manifest.
- Removes `@font-face` rules from CSS files and from `<style>` blocks.
- Removes font obfuscation entries from `META-INF/encryption.xml` (deletes the file if it
  becomes empty).
- Sets Apple Books' `specified-fonts` option to `false`.

**Font families**
- Removes every `font-family` declaration, in stylesheets, `<style>` blocks and inline
  `style="…"` attributes, so the reader's font is used everywhere. Even system fonts like
  `Georgia` or `"Times New Roman"` differ a lot in apparent size, so this alone makes books
  look more alike.
- Declarations that include `monospace` become just `monospace`, so code stays fixed-width.

**Font sizes**
- Converts fixed sizes (`font-size: 11pt`, `14px`, …) to `em` (16px = 12pt = 1em). Many
  readers ignore their own size setting for fixed sizes.
- Makes the main body text exactly the reader's default size, without any `font-size` rule
  on it. It works out which size most of the book's text is rendered at by following the CSS
  cascade (`html`/`body`, classes, descendant selectors, inline styles), then removes the
  `font-size` declarations that produce it.
  - This includes rules that cancel out, like `body { font-size: 1.29em }` with
    `p { font-size: 0.77em }`. Many reading apps override the `body` size, which would leave
    that text at 77% of your setting.
  - Other sizes (headings, footnotes, block quotes, …) are rescaled where needed, so they
    keep the same size relative to the main text.
- Expands the `font:` shorthand into separate properties so the rules above can apply.

**Line height**
- Removes the `line-height` declarations that apply to the main text, so the reader's line
  spacing setting applies. Headings keep theirs.
- Removes fixed line heights (`px`, `pt`, …) everywhere: they don't grow with the font size,
  so lines overlap when you enlarge the text.

**Paragraphs**
- Gives main-text paragraphs the usual book style: a 1em first-line indent and no space
  between paragraphs.
- A paragraph that doesn't follow another main-text paragraph starts flush left (no indent).
  This covers the first paragraph after a chapter heading, a scene break (`<hr/>`, `* * *`)
  or an image.
- Centered or right-aligned paragraphs keep their layout. So do paragraph styles that hold
  less than 2% of the text, such as poetry, letters or epigraphs.
- The new values are set as inline `style` attributes on each paragraph, so they win over
  the book's stylesheets without touching rules other elements share.

Everything else is copied byte-for-byte. `mimetype` stays the first, uncompressed entry.

DRM-protected books (Adobe `rights.xml`, or `encryption.xml` entries that are not font
obfuscation) are refused with an error; the tool cannot edit encrypted content.

## Limitations

- The analysis ignores selectors with pseudo-classes, attribute selectors and sibling
  combinators (`p:first-child`, `p[lang]`, `h1 + p`). Those rules are left unchanged, so a
  book that styles its first paragraphs with `h1 + p` may still get those paragraphs'
  indent from the inline style rather than from that rule.
- Side margins and text alignment are not changed.
- CSS is edited with regular expressions, not a full parser. Unusual CSS (e.g. `}` inside a
  string within an `@font-face` block) may not be handled.

## Tests

```bash
python3 -m unittest -v
```

## License

[MIT](LICENSE)
