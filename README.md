# StockWatch.pl Announcement Scraper

A Python scraper that collects company announcements from
[StockWatch.pl](https://www.stockwatch.pl/komunikaty-spolek/wszystkie.aspx)
and downloads their PDF attachments.

The script fetches announcement listings, visits each announcement detail page,
extracts and downloads available PDF reports, and exports the collected data to
`announcements.json` and `announcements.csv`.

## What the scraper does

The scraper follows a complete end-to-end pipeline:

1. **Fetches listing pages** from StockWatch.pl.
2. **Parses announcement rows**, extracting:
   - Company name
   - Announcement title
   - Category (e.g., ESPI, EBI, Walne Zgromadzenie)
   - Publication timestamp
   - Detail page URL
3. **Processes each announcement**:
   - Skips announcement URLs that have already been processed.
   - Fetches the announcement's detail page.
   - Locates PDF attachment links within the report body.
   - Downloads each PDF attachment directly into the `attachments/` folder.
   - Skips downloading if the file already exists on disk.
4. **Collects results in real time** until the target record count (default: 100)
   or the maximum number of listing pages is reached.
5. **Sorts and deduplicates** the final records by publication date and saves the
   output files.

## Output files

After running, the output directory contains:

| File or directory | Description |
| ----------------- | ----------- |
| `announcements.json` | Complete metadata for all collected announcements, including the full list of attachments for each record. |
| `announcements.csv` | Summary table containing announcement details and information on the primary PDF attachment. |
| `attachments/` | Folder containing all downloaded PDF files. |

## Pipeline overview

``` text
                     Fetch Listing Page
                             │
                     Parse Table Rows
                             │
            ┌────────────────┴────────────────┐
            ▼                                 ▼
   For each announcement:             Page complete?
   1. Check if URL already seen       Fetch next listing page
   2. Fetch detail page               (until --target or
   3. Find PDF links                  --max-pages reached)
   4. Download PDFs
   5. Record announcement & status
            │
            ▼
    Target reached (100) / End
            │
            ▼
   Sort -> Deduplicate -> Save
    (JSON, CSV, attachments/)
```

## Requirements

- Python 3.10 or newer
- Required libraries:
  - `requests`
  - `PySocks` (for SOCKS proxy support)
  - `lxml`
- A network route that can reach StockWatch.pl. The website restricts access
  from outside Poland, so a working Polish IP, VPN, or proxy is required.

## Installation and quick start

1. Install the required packages:

``` bash
pip install -r requirements.txt
```

2. Run the scraper:

``` bash
python raw_stockwatch.py
```

By default, the script collects up to 100 unique announcements across up to 6
listing pages using the preconfigured Polish SOCKS4 proxy.

## Command-line options

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--target N` | `100` | Target number of unique records to collect |
| `--max-pages N` | `6` | Maximum number of listing pages to browse |
| `--delay SEC` | `0.5` | Politeness delay (in seconds) between requests |
| `--proxy URL` | `socks4://185.147.69.48:1080` | Proxy URL to route requests through (pass `""` to disable) |
| `--output DIR` | Script directory | Directory where JSON, CSV, and downloaded attachments are stored |

### Examples

Run with default settings:

``` bash
python raw_stockwatch.py
```

Collect 50 announcements with a 1-second delay between requests:

``` bash
python raw_stockwatch.py --target 50 --delay 1.0
```

Browse up to 10 listing pages:

``` bash
python raw_stockwatch.py --max-pages 10
```

Save results to a custom directory:

``` bash
python raw_stockwatch.py --output /path/to/output_dir
```

Disable the proxy (when running from a Polish IP or active Polish VPN):

``` bash
python raw_stockwatch.py --proxy ""
```

Use a custom proxy (HTTP, SOCKS4, or SOCKS5):

``` bash
python raw_stockwatch.py --proxy socks5://user:pass@host:1080
```

## How announcement data is extracted

The scraper reads table rows from each listing page. A row qualifies as a valid
announcement when it contains at least five table cells and both the company
and title cells contain anchor tags (`<a>`).

For each row, it extracts:
- `company_name`: Company ticker/name.
- `title`: Announcement headline.
- `category`: Filing category or reporting standard (ESPI, EBI, etc.).
- `published_at`: Date and time of publication.
- `url`: Full URL to the announcement detail page.

Rows missing any required field are skipped.

## How PDF links are found

The scraper uses `lxml` with XPath selectors to locate attachment links on each
announcement detail page:

1. **Scoped search**: Inspects `//article//a[@href]` first. Scoping the query
   to `<article>` ensures only attachments inside the report body are matched,
   ignoring navigation headers, sidebars, and footer links.
2. **Fallback search**: If no `<article>` container is present, searches `//a[@href]`.

A link is identified as a PDF attachment if:
- The URL ends with `.pdf` (case-insensitive); or
- The URL points to `pliki.aspx` (StockWatch.pl's attachment download endpoint); or
- The anchor text contains attachment keywords (`pdf`, `załącznik`, `pobierz`,
  `download`, `skan`, `dokument`) and points to a file path.

Duplicate attachment URLs on the same page are automatically filtered out.

## PDF filenames

Each downloaded PDF is given a clean, descriptive filename using the format:

``` text
{company}_{date}_{announcement_id}_{attachment_index}.pdf
```

Example filenames:
``` text
monnari_20261021_20261021_174524_0000360839_0.pdf
techrobot_20261021_20261021_174004_0000005405_0.pdf
techrobot_20261021_20261021_174004_0000005405_1.pdf
```

- `company`: Sanitized lowercase company name.
- `date`: YYYYMMDD extracted from `published_at`.
- `announcement_id`: Unique identifier parsed from the detail page URL.
- `attachment_index`: 0-based index distinguishing multiple PDFs in a single announcement.

## Output formats

### `announcements.json`

Stores the complete structured output with full attachment lists for every record.

Example record:

``` json
[
  {
    "company_name": "MONNARI",
    "title": "Kupno akcji własnych",
    "category": "Wejście znaczącego inwestora lub insidera",
    "published_at": "2026-10-21 17:45",
    "url": "https://www.stockwatch.pl/komunikaty-spolek/monnari,kupno-akcji-wlasnych-2026-10-21,espi,20261021_174524_0000360839",
    "attachments": [
      {
        "url": "https://www.stockwatch.pl/komunikaty-spolek/pliki.aspx?type=espi&file=zestawienie-skup-21102026.pdf&id=20261021_174524_0000360839",
        "filename": "monnari_20261021_20261021_174524_0000360839_0.pdf",
        "downloaded": true,
        "text": "Zestawienie skup 21.10.2026.pdf"
      }
    ]
  }
]
```

### `announcements.csv`

Tabular export formatted with the following columns:

``` text
company_name, title, category, published_at, url, pdf_url, pdf_file
```

`pdf_url` and `pdf_file` list the primary (first) PDF attachment for quick inspection.

## Deduplication and limits

- **In-flight deduplication**: A `seen_urls` set ensures the scraper does not
  fetch or download duplicate announcement URLs across pages.
- **Record deduplication**: Records are deduplicated by `(company_name, title, published_at)`.
- **Target limit**: The scraper halts as soon as `--target` records have been collected.

## Retries and resume support

- **Retries**: PDF downloads are retried up to 2 times with exponential backoff
  if connection or timeout errors occur.
- **Resume capability**: If an attachment already exists on disk in `attachments/`
  with non-zero size, downloading is skipped and the file is marked as cached.
- **Graceful shutdown**: Pressing `Ctrl+C` cleanly saves all announcements and
  attachments downloaded up to that point before exiting.

## Troubleshooting

### `HTTP 403 (geo-blocked)`
StockWatch.pl blocks traffic from outside Poland.
- Verify that your proxy or Polish VPN connection is active.
- To disable the default proxy when on a Polish IP, use `--proxy ""`.

### No attachments found
- Verify whether the announcement actually includes downloadable attachments.
- Check if the page layout has changed from the expected `<article>` structure.

## Project layout

``` text
.
├── raw_stockwatch.py      # Main scraper & PDF downloader
├── requirements.txt       # Project dependencies
├── README.md              # Documentation
├── announcements.json     # Full JSON dataset
├── announcements.csv      # Summary CSV table
└── attachments/           # Downloaded PDF files
```

