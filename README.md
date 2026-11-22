# epub-normalize-fonts

Makes EPUB books follow your e-reader's own font and size settings. It removes embedded
fonts, removes the publisher's font choices, and brings the main text to your reader's
default size. Single file, Python standard library only.

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
- Makes the main body text exactly the reader's default size. It works out which size most
  of the book's text is rendered at by following the CSS cascade (`html`/`body`, classes,
  descendant selectors, inline styles). If that differs from 100% by more than 3%, it removes
  the `font-size` declarations responsible. Headings, footnotes, block quotes and so on keep
  their own sizes, because only the rules that set the main text size are touched.
- Expands the `font:` shorthand into separate properties so the rules above can apply.

Everything else is copied byte-for-byte. `mimetype` stays the first, uncompressed entry.

DRM-protected books (Adobe `rights.xml`, or `encryption.xml` entries that are not font
obfuscation) are refused with an error; the tool cannot edit encrypted content.

## Limitations

- The body size analysis ignores selectors with pseudo-classes, attribute selectors and
  sibling combinators (`p:first-child`, `p[lang]`, `h1 + p`). Those rules are left unchanged.
- `line-height`, margins and text alignment are not changed.
- CSS is edited with regular expressions, not a full parser. Unusual CSS (e.g. `}` inside a
  string within an `@font-face` block) may not be handled.

## Tests

```bash
python3 -m unittest -v
```

## License

[MIT](LICENSE)
