# jusTokenMax benchmark results

_Token counts: Markdown side via `tiktoken/cl100k`; PDF 'before' via Anthropic page-image model (~1500 tok/page)._


## PDF -> Markdown

| file | pages | tokens before | tokens after | reduction |
| --- | ---: | ---: | ---: | ---: |
| sample-10page.pdf | 10 | 21,960 | 6,960 | **-68%** |
| sample-30page.pdf | 30 | 65,889 | 20,889 | **-68%** |
| **total** | | **87,849** | **27,849** | **-68%** |

## Image compression

| file | orig px | new px | bytes before | bytes after | bytes saved | base64 tokens before→after |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| _deckimg0.png | 32x32 | 32x32 | 114 | 292 | **--157%** | 38 → 97 |
| _deckimg4.png | 32x32 | 32x32 | 116 | 293 | **--153%** | 38 → 97 |
| _deckimg8.png | 32x32 | 32x32 | 114 | 293 | **--158%** | 38 → 97 |
| sample-screenshot.png | 3000x2000 | 1568x1045 | 189,466 | 106,996 | **-43%** | 63,155 → 35,665 |

_Image note: native-vision models downscale to <=1568px anyway, so the byte savings translate to token savings only in pipelines that inline images as base64._


## Log compression

| file | lines before | lines after | tokens before | tokens after | reduction |
| --- | ---: | ---: | ---: | ---: | ---: |
| sample-build.log | 4,345 | 21 | 107,668 | 396 | **-99%** |

## JSON / structured-output compression

| file | tokens before | tokens after | reduction |
| --- | ---: | ---: | ---: |
| sample-response.json | 168,023 | 374 | **-99%** |

## Notebook / CSV / delta

| input | tokens before | tokens after | reduction |
| --- | ---: | ---: | ---: |
| notebook (20 cells, image outputs) | 401,170 | 610 | **-99%** |
| CSV (5,000 rows) | 57,340 | 237 | **-99%** |
| delta re-read (1 edit in 600 lines) | 2,407 | 88 | **-96%** |
| HTML page (script/style/nav chrome) | 6,939 | 606 | **-91%** |
| SVG diagram (labels in reading order) | 10,487 | 283 | **-97%** |

## Code index (read symbols, not files)

Indexed **598 symbols** across **51 files**. Cost to locate a symbol, summed over 51 lookups:

| approach | tokens |
| --- | ---: |
| read each whole file | 75,641 |
| one `justokenmax query` hit each | 1,275 |
| **reduction** | **-98%** |
