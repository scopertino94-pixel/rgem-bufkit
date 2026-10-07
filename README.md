# RGEM to BUFKIT

Converts Canadian RDPS (regional GEM, 10 km) GRIB2 output from the MSC datamart
(`dd.weather.gc.ca`) into BUFKIT `.buf` files and publishes them for download.
This work was compiled by various others who have attempted something like 
this in the past. Everything here is free for public use. Please cite accordingly.

The model shows up in BUFKIT as RGEM. BUFKIT v19 reserves the name "RDPS" for
its own Canadian handling and throws run-time error 13 if generic `.buf` files
are loaded under it, so don't rename it.

One machine (the "producer") builds and publishes the files. Everyone else runs a
small Perl script that downloads the finished `.buf` files into BUFKIT. No Python
on the consumer side.

## Layout

```
code/
  bufkit_replica/      .buf read/write library + shared index routines
  tools/               command-line entry points
  samples/rgem/        station list with coordinates
  requirements.txt     Python dependencies
scripts/               operator/consumer scripts (.bat, .pl)
```

## What each file does

| File | Purpose |
|---|---|
| `code/tools/update_rgem.py` | Main entry point. Finds the latest RDPS cycle, downloads every grid once, pulls out all stations, writes `rgem_*.buf`, and optionally publishes. The only script you schedule. |
| `code/tools/rdps_probe.py` | Datamart URLs and file naming, the downloader, and `build_sounding()` (one forecast hour of point data to a BUFKIT sounding). Imported by `update_rgem.py`. Run it directly to build one test station (KBED). |
| `code/bufkit_replica/writer.py` | Writes soundings to BUFKIT `.buf` text. Format-critical (see Constraints). |
| `code/bufkit_replica/parser.py` | Reads a `.buf` file back into objects. Also supplies the data classes the writer uses. |
| `code/bufkit_replica/rrfs_bufr.py` | Written for the RRFS version of this tool. RGEM only uses its convective index routines (CAPE, LIFT, SWET, BRCH, LFC/EL) and the RH cloud fraction. |
| `code/bufkit_replica/grib_to_buf.py` | Field order tables (SNPARM / STNPRM / surface) shared by the writer and decoder. Required. |
| `code/bufkit_replica/__init__.py` | Package init; exposes the parser. |
| `code/samples/rgem/station_coords.json` | Stations to produce (166) with lat/lon and station number. Add or remove stations here. |
| `scripts/RGEM Update Dataset.bat` | Runs the producer. Edit the two paths and the publish target at the top. |
| `scripts/WW Bufkit RGEM.pl` | Consumer downloader. Pulls the published `.buf` files into the BUFKIT Data folder. Set `$REPO` to the host. |
| `scripts/Setup RGEM in BUFKIT.pl` | Run once per machine. Adds an `RGEM` line to the BUFKIT model menu. |

## Requirements

Python 3.11. Install eccodes via conda, then the rest with pip:

```
conda install -c conda-forge eccodes python-eccodes
pip install -r code/requirements.txt
```

## Run the producer

```
python code/tools/update_rgem.py --outdir <BUFKIT Data dir> --publish-repo ORG/rgem-bufkit
```

Finds the newest RDPS cycle (00/06/12/18Z) that has finished posting. If it is
new, it downloads about 5,000 GRIB files (roughly 3 minutes), writes
`rgem_*.buf`, publishes, and exits. If the cycle was already processed it exits
in a few seconds. Schedule it as often as you like.

| Flag | Meaning |
|---|---|
| `--outdir <dir>` | Output directory (default `code/out/rgem`). |
| `--publish-repo <id>` | Hugging Face dataset to upload to. Omit to write local files only. |
| `--fhx 0,3,...,84` | Forecast hours to build (default every 3 h to 84 h). |
| `--limit N` | Only do the first N stations. For testing. |
| `--workers N` | Parallel processes for reading grids (default 6). |
| `--statefile <path>` | Where the last finished cycle is recorded (default `code/tools/update_rgem.state`). |
| `--force` | Reprocess even if the cycle was already done. |

Downloaded grids go to `code/cache/rdps/` and are cleared down to the current
cycle at the start of each run, so the cache doesn't grow. Downloads time out
after 60 s and retry up to 3 times; a file is only kept if it is a complete
GRIB message.

Publishing requires `hf auth login`. The producer squashes the dataset history
each run, so storage stays flat.

## Consumers

- `scripts/Setup RGEM in BUFKIT.pl` - run once to add RGEM to the BUFKIT menu.
- `scripts/WW Bufkit RGEM.pl` - run each cycle to download the latest `.buf` files.
  Set `$REPO` to wherever the producer publishes. Needs only BUFKIT, Perl, and curl.

## Constraints (do not break these or BUFKIT will crash)

The writer and sounding builder enforce these. They are not optional.

1. `.buf` files must use CRLF (`\r\n`) line endings.
2. `STIM` increments per sounding (0, 1, 2, ...).
3. Every level must satisfy `Td <= Tw <= T`.
4. Every sounding in a file must have the same number of levels. Levels below
   the lowest surface pressure of the run are dropped for that reason.
5. `CFRL` and `OMEG` must be populated on every level, never all-missing. RDPS
   only has omega at 4 levels; the rest are interpolated.
6. `LCLT` is written in Kelvin, not Celsius.
7. `LFCT` and `EQLV` are paired - emit both or neither.
8. `WSYM` (present weather) must be set, 999 for none. Missing shows as
   "Error Wx = -9999" in BUFKIT.

When changing the writer or sounding builder, re-test the output in BUFKIT.

## Known gaps

- `C01M` (convective precip) is left missing.
- Station list and the downloader's `@sites` list are kept by hand; if you add a
  station to `station_coords.json`, add it to `WW Bufkit RGEM.pl` too.
