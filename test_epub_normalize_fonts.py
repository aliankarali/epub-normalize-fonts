import io
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import epub_normalize_fonts as enf

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="id">urn:uuid:1234</dc:identifier>
    <meta property="ibooks:specified-fonts">true</meta>
  </metadata>
  <manifest>
    <item id="css" href="styles/style.css" media-type="text/css"/>
    <item id="ch1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="f1" href="fonts/Fancy%20Serif.ttf" media-type="font/ttf"/>
    <item id="f2" href="fonts/mono.otf" media-type="application/vnd.ms-opentype"></item>
  </manifest>
  <spine><itemref idref="ch1"/></spine>
</package>"""

PLAIN_OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="id">urn:uuid:5678</dc:identifier>
  </metadata>
  <manifest>
    <item id="css" href="styles/style.css" media-type="text/css"/>
    <item id="ch1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="ch1"/></spine>
</package>"""

CSS = """@font-face {
  font-family: "Fancy Serif";
  src: url(../fonts/Fancy%20Serif.ttf);
}
@font-face { font-family: 'Mono X'; src: url(../fonts/mono.otf) }
body {
  font-family: "Fancy Serif", Georgia, serif;
  margin: 0;
}
h1 { font-family: 'Mono X' !important }
p { font-family: Georgia; color: black; }
"""

XHTML = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <link rel="stylesheet" href="../styles/style.css"/>
  <style>
    @font-face { font-family: Inline Face; src: url(../fonts/extra.woff); }
    .x { font-family: "Inline Face"; }
  </style>
</head>
<body>
  <p style="font-family: &quot;Fancy Serif&quot;, serif; color: red">Hello</p>
  <p style='font-family: Georgia'>World — ünïcödé</p>
</body>
</html>"""

ENCRYPTION_FONTS_ONLY = """<?xml version="1.0"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
            xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>
    <enc:CipherData><enc:CipherReference URI="OEBPS/fonts/Fancy%20Serif.ttf"/></enc:CipherData>
  </enc:EncryptedData>
</encryption>"""

ENCRYPTION_DRM = """<?xml version="1.0"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
            xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>
    <enc:CipherData><enc:CipherReference URI="OEBPS/text/ch1.xhtml"/></enc:CipherData>
  </enc:EncryptedData>
</encryption>"""

APPLE = """<?xml version="1.0" encoding="UTF-8"?>
<display_options><platform name="*"><option name="specified-fonts">true</option></platform></display_options>"""

LOREM = ("Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor "
         "incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud "
         "exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat. ")


def make_epub(path: Path, encryption: str | None = ENCRYPTION_FONTS_ONLY, fonts: bool = True) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        if fonts:
            z.writestr("META-INF/com.apple.ibooks.display-options.xml", APPLE)
        if encryption:
            z.writestr("META-INF/encryption.xml", encryption)
        z.writestr("OEBPS/content.opf", OPF if fonts else PLAIN_OPF)
        z.writestr("OEBPS/styles/style.css", CSS if fonts else "p { color: red }\n")
        z.writestr("OEBPS/text/ch1.xhtml", XHTML if fonts else "<html/>")
        if fonts:
            z.writestr("OEBPS/fonts/Fancy Serif.ttf", b"\x00" * 5000)
            z.writestr("OEBPS/fonts/mono.otf", b"\x00" * 3000)
            z.writestr("OEBPS/fonts/extra.woff", b"\x00" * 1000)
    return path


def make_book(path: Path, css: str, body: str, body_attrs: str = "") -> Path:
    opf = PLAIN_OPF.replace("styles/style.css", "style.css")
    html = f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>T</title><link rel="stylesheet" type="text/css" href="../style.css"/></head>
<body{body_attrs}>
{body}
</body>
</html>"""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/style.css", css)
        z.writestr("OEBPS/text/ch1.xhtml", html)
    return path


def read(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n).decode("utf-8") for n in z.namelist()}


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = enf.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class EpubTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.book = make_epub(self.dir / "book.epub")

    def tearDown(self):
        self.tmp.cleanup()

    def test_removes_fonts_and_font_families(self):
        out = self.dir / "out.epub"
        report = enf.normalize_epub(self.book, out)
        self.assertEqual(len(report.font_files), 3)
        self.assertEqual(report.font_bytes, 9000)
        self.assertEqual(report.font_faces, 3)

        files = read(out)
        self.assertFalse(any(n.endswith((".ttf", ".otf", ".woff")) for n in files))
        self.assertNotIn("META-INF/encryption.xml", files)

        opf = files["OEBPS/content.opf"]
        self.assertNotIn("fonts/", opf)
        self.assertIn('href="styles/style.css"', opf)
        self.assertIn('<meta property="ibooks:specified-fonts">false</meta>', opf)
        self.assertIn(">false</option>", files["META-INF/com.apple.ibooks.display-options.xml"])

        css = files["OEBPS/styles/style.css"]
        self.assertNotIn("@font-face", css)
        self.assertNotIn("font-family", css)
        self.assertIn("body {\n  margin: 0;\n}", css)
        self.assertIn("h1 { }", css)
        self.assertIn("p { color: black; }", css)

        html = files["OEBPS/text/ch1.xhtml"]
        self.assertNotIn("@font-face", html)
        self.assertNotIn("font-family", html)
        self.assertIn('<p style="color: red">Hello</p>', html)
        self.assertIn("<p>World — ünïcödé</p>", html)

    def test_mimetype_first_and_stored(self):
        out = self.dir / "out.epub"
        enf.normalize_epub(self.book, out)
        with zipfile.ZipFile(out) as z:
            first = z.infolist()[0]
            self.assertEqual(first.filename, "mimetype")
            self.assertEqual(first.compress_type, zipfile.ZIP_STORED)
            self.assertIsNone(z.testzip())

    def test_keeps_non_font_encryption_entries(self):
        enc = ENCRYPTION_FONTS_ONLY.replace(
            "</encryption>",
            """  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>
    <enc:CipherData><enc:CipherReference URI="OEBPS/other.bin"/></enc:CipherData>
  </enc:EncryptedData>
</encryption>""",
        )
        book = make_epub(self.dir / "enc.epub", encryption=enc)
        out = self.dir / "out.epub"
        enf.normalize_epub(book, out)
        enc_out = read(out)["META-INF/encryption.xml"]
        self.assertIn("other.bin", enc_out)
        self.assertNotIn("Fancy", enc_out)

    def test_drm_is_refused(self):
        book = make_epub(self.dir / "drm.epub", encryption=ENCRYPTION_DRM)
        with self.assertRaises(enf.EpubError):
            enf.normalize_epub(book, self.dir / "out.epub")

    def test_cli_suffix_force_and_dry_run(self):
        code, out, _ = run("-n", str(self.book))
        self.assertEqual(code, 0)
        self.assertIn("[dry run]", out)
        self.assertFalse((self.dir / "book-normalized.epub").exists())

        code, out, _ = run(str(self.book))
        self.assertEqual(code, 0)
        self.assertTrue((self.dir / "book-normalized.epub").exists())
        self.assertIn("removed 3 font file(s)", out)

        code, _, err = run(str(self.book))
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertEqual(run("-f", str(self.book))[0], 0)

    def test_cli_directory_skips_own_outputs(self):
        run(str(self.dir))
        code, out, _ = run("-f", str(self.dir))
        self.assertEqual(code, 0)
        self.assertNotIn("normalized-normalized", out)
        self.assertFalse((self.dir / "book-normalized-normalized.epub").exists())

    def test_cli_in_place_and_output_dir(self):
        outdir = self.dir / "clean"
        self.assertEqual(run("-o", str(outdir), str(self.book))[0], 0)
        self.assertNotIn("OEBPS/fonts/mono.otf", read(outdir / "book.epub"))

        self.assertEqual(run("-i", str(self.book))[0], 0)
        self.assertNotIn("OEBPS/fonts/mono.otf", read(self.book))
        code, out, _ = run("-i", str(self.book))
        self.assertIn("nothing to change", out)

    def test_book_needing_no_changes_is_copied(self):
        book = make_epub(self.dir / "plain.epub", encryption=None, fonts=False)
        code, out, _ = run(str(book))
        self.assertEqual(code, 0)
        self.assertIn("nothing to change", out)
        self.assertEqual((self.dir / "plain-normalized.epub").read_bytes(), book.read_bytes())

    def test_bad_file_reports_error(self):
        bad = self.dir / "bad.epub"
        bad.write_text("not a zip")
        code, _, err = run(str(bad))
        self.assertEqual(code, 1)
        self.assertIn("error:", err)


class CssTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def normalize(self, css, body, body_attrs=""):
        book = make_book(self.dir / "b.epub", css, body, body_attrs)
        out = self.dir / "out.epub"
        report = enf.normalize_epub(book, out)
        files = read(out) if out.exists() else read(book)
        return report, files["OEBPS/style.css"], files["OEBPS/text/ch1.xhtml"]

    def test_font_shorthand_is_expanded(self):
        css = '@font-face { font-family: "Fancy Serif"; src: url(a.ttf) }\n' \
              'p { font: italic 12pt/1.4 "Fancy Serif", serif; }\n'
        _, out, _ = self.normalize(css, f"<p>{LOREM * 3}</p>")
        self.assertIn("p { font-style: italic; font-variant: normal; font-weight: normal; "
                      "font-size: 1em; line-height: 1.4; }", out)

    def test_monospace_is_kept(self):
        css = 'body { font-family: Georgia, serif }\ncode { font-family: "Courier New", monospace }\n'
        _, out, _ = self.normalize(css, f"<p>{LOREM}</p>")
        self.assertIn("body { }", out)
        self.assertIn("code { font-family: monospace }", out)

    def test_small_body_rule_is_neutralized_headings_kept(self):
        css = "body { font-size: 0.8em; margin: 0 }\nh1 { font-size: 1.5em }\n.note { font-size: 0.8em }\n"
        body = f"<h1>Title</h1><p>{LOREM * 3}</p><p class='note'>A short note.</p>"
        report, out, _ = self.normalize(css, body)
        self.assertIn("body { margin: 0 }", out)
        self.assertIn("h1 { font-size: 1.5em }", out)
        self.assertIn(".note { font-size: 0.8em }", out)
        self.assertAlmostEqual(report.body_before, 0.8)
        self.assertAlmostEqual(report.body_after, 1.0)

    def test_class_on_paragraphs(self):
        css = "p.text { font-size: 1.2em; text-indent: 1em }\n.aside p { font-size: 0.7em }\n"
        body = f"<p class='text'>{LOREM * 3}</p><div class='aside'><p>{LOREM}</p></div>"
        _, out, _ = self.normalize(css, body)
        self.assertIn("p.text { text-indent: 1em }", out)
        self.assertIn(".aside p { font-size: 0.7em }", out)

    def test_balanced_sizes_left_alone(self):
        css = ".calibre { font-size: 1.25em }\n.calibre1 { font-size: 0.8em }\n"
        body = f"<p class='calibre1'>{LOREM * 3}</p>"
        report, out, _ = self.normalize(css, body, " class='calibre'")
        self.assertFalse(report.changed)
        self.assertEqual(out, css)

    def test_descendant_rule(self):
        css = "div.chapter p { font-size: 0.9em }\n"
        body = f"<div class='chapter'><p>{LOREM * 3}</p></div>"
        _, out, _ = self.normalize(css, body)
        self.assertIn("div.chapter p { }", out)

    def test_absolute_sizes_become_relative(self):
        css = "p { font-size: 11pt }\nh2 { font-size: 18px }\n"
        body = f"<h2>Part</h2><p>{LOREM * 3}</p>"
        report, out, _ = self.normalize(css, body)
        self.assertIn("p { }", out)
        self.assertIn("h2 { font-size: 1.125em }", out)
        self.assertEqual(report.sizes_converted, 2)

    def test_inline_sizes(self):
        css = "p { margin: 0 }\n"
        body = (f"<p style=\"font-size: 14pt; color: red\">{LOREM}</p>"
                f"<p style='font-size: 14pt'>{LOREM * 2}</p>")
        _, _, html = self.normalize(css, body)
        self.assertIn('<p style="color: red">', html)
        self.assertNotIn("font-size", html)

    def test_cli_is_idempotent(self):
        css = "body { font-family: Georgia; font-size: 12px }\n"
        book = make_book(self.dir / "b.epub", css, f"<p>{LOREM * 3}</p>")
        code, out, _ = run("-i", str(book))
        self.assertEqual(code, 0)
        self.assertIn("body text 75% -> 100%", out)
        self.assertIn("body { }", read(book)["OEBPS/style.css"])
        code, out, _ = run("-i", str(book))
        self.assertIn("nothing to change", out)


if __name__ == "__main__":
    unittest.main()
