# Magnifica Humanitas EPUB

Clean EPUB version of Pope Leo XIV's *Magnifica Humanitas*, generated from the Holy See's HTML source.

Available in English and Polish. Sources:

- English: https://www.vatican.va/content/leo-xiv/en/encyclicals/documents/20260515-magnifica-humanitas.html
- Polish: https://www.vatican.va/content/leo-xiv/pl/encyclicals/documents/20260515-magnifica-humanitas.html

The EPUB includes:

- cover image
- metadata
- table of contents
- footnotes

It validates with `epubcheck` 5.3.0 with no errors or warnings.

## Build

Dependencies are managed with [pixi](https://pixi.sh). Install it once, then:

```sh
# English
pixi run python build_magnifica_humanitas_epub.py --config en.yaml

# Polish
pixi run python build_magnifica_humanitas_epub.py --config pl.yaml

# Validate
epubcheck "Magnifica Humanitas - Pope Leo XIV.epub"
```

`pixi run` installs the environment on first use — no manual `pip install` needed.

## Other languages

Add a new YAML config (use `en.yaml` as a template). Key fields:

| Field | Description |
|---|---|
| `lang` | BCP 47 language tag (`en`, `pl`, …) |
| `source_url` | Vatican page URL for that language |
| `source_html` | Local cache file for the downloaded HTML |
| `output_epub` | Output filename |
| `intro_anchor` | `<a name="">` that marks the document body start — inspect the downloaded HTML if the build fails |
| `top_level_pattern` | Regex matching level-1 heading text in the TOC |
| `cover_label`, `cover_date`, `toc_title_page`, `notes_heading` | Localised UI strings |

Then run:

```sh
pixi run python build_magnifica_humanitas_epub.py --config your-lang.yaml
```
