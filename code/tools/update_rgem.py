"""
RDPS (RGEM) -> BUFKIT for every station in samples/rgem/station_coords.json.

Downloads each GRIB grid once per cycle and pulls every station's point out of
it, so run time depends on the number of grids, not the number of stations.
Writes rgem_<id>.buf per station and optionally publishes the folder to a
Hugging Face dataset (history squashed each run so the repo doesn't grow).

URL conventions, downloads and the sounding builder come from rdps_probe.py.

Usage:
  python tools/update_rgem.py [--outdir DIR] [--limit N] [--fhx 0,3,..,84]
                              [--workers N] [--statefile PATH] [--force]
                              [--publish-repo ORG/rgem-bufkit]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import warnings
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import xarray as xr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
from rdps_probe import (CACHE, LEVELS, ISBL_VARS, SNPARM, STNPRM, SFC_PARAMS,  # noqa: E402
                        fname, url_for, fetch, detect_cycle, build_sounding, GRID)
from bufkit_replica.parser import BufkitFile  # noqa: E402
from bufkit_replica.writer import write_buf  # noqa: E402

COORDS = os.path.join(ROOT, "samples", "rgem", "station_coords.json")
SURFACE_FIELDS = [("AirTemp", "AGL-2m"), ("DewPoint", "AGL-2m"),
                  ("WindU", "AGL-10m"), ("WindV", "AGL-10m"),
                  ("Pressure", "MSL"), ("Pressure", "Sfc"),
                  ("GeopotentialHeight", "Sfc"), ("PrecipRate", "Sfc"),
                  ("Rain-Accum3h", "Sfc"), ("Snow-Accum3h", "Sfc"),
                  ("FreezingRain-Accum3h", "Sfc"), ("IcePellets-Accum3h", "Sfc")]


def read_field(path, iys, ixs):
    """Read one single-message GRIB grid and return values at many (iy,ix) points."""
    ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
    v = list(ds.data_vars)[0]
    arr = ds[v].values
    ds.close()
    return arr[iys, ixs].astype(float)


# --- grid extraction in a process pool ---
# eccodes isn't thread-safe, so this uses processes. These are module-level
# functions so they pickle under Windows spawn; station indices are passed in
# through the initializer.
_PP_IYS = _PP_IXS = None


def _pp_init(iys, ixs):
    global _PP_IYS, _PP_IXS
    _PP_IYS, _PP_IXS = iys, ixs


def _pp_read(job):
    key, path = job
    if not os.path.exists(path):
        return key, None
    try:
        ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
        v = list(ds.data_vars)[0]
        arr = ds[v].values
        ds.close()
        return key, arr[_PP_IYS, _PP_IXS].astype(float)
    except Exception:
        return key, None


def station_indices(ref_path, lats, lons):
    """Nearest grid (iy,ix) for each station; the grid is shared by all files."""
    ds = xr.open_dataset(ref_path, engine="cfgrib", backend_kwargs={"indexpath": ""})
    glat = ds.latitude.values
    glon = ds.longitude.values
    ds.close()
    iys, ixs = [], []
    for lat0, lon0 in zip(lats, lons):
        lon0n = lon0 % 360
        d2 = (glat - lat0) ** 2 + (((glon - lon0n + 180) % 360) - 180) ** 2
        iy, ix = np.unravel_index(np.argmin(d2), glat.shape)
        iys.append(int(iy)); ixs.append(int(ix))
    return np.array(iys), np.array(ixs)


def run(outdir, fxx_list, limit=None, publish_repo=None, workers=8,
        statefile=None, force=False):
    os.makedirs(CACHE, exist_ok=True)
    date_ymd, hh = detect_cycle()
    cyc_id = f"{date_ymd}{hh:02d}"

    # statefile gate: skip if this cycle was already produced (scheduled-job no-op)
    statefile = statefile or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "update_rgem.state")
    if not force and os.path.exists(statefile) and open(statefile).read().strip() == cyc_id:
        print(f"cycle {cyc_id} already processed (statefile) - no-op")
        return 0

    coords = json.load(open(COORDS))
    sids = list(coords)
    if limit:
        sids = sids[:limit]
    lats = [coords[s]["lat"] for s in sids]
    lons = [coords[s]["lon"] for s in sids]
    stnms = [coords[s]["stnm"] for s in sids]
    stids = [coords[s]["stid"] for s in sids]
    cycle_dt = pd.Timestamp(f"{date_ymd} {hh:02d}:00")
    print(f"RGEM producer: cycle {cyc_id}Z, {len(sids)} stations, "
          f"fxx={fxx_list[0]}..{fxx_list[-1]} ({len(fxx_list)} times)")

    # drop any earlier cycle's grids so the cache holds one cycle at most
    keep_tag = f"{date_ymd}T{hh:02d}Z"
    pruned = 0
    for f in os.listdir(CACHE):
        if keep_tag not in f:
            try:
                os.remove(os.path.join(CACHE, f)); pruned += 1
            except OSError:
                pass
    if pruned:
        print(f"pruned {pruned} stale cache files", flush=True)

    # ---- download every grid once ----
    jobs = []
    for fff in fxx_list:
        for var in ISBL_VARS:
            for l in LEVELS:
                u = url_for(date_ymd, hh, var, f"IsbL-{l:04d}", fff)
                jobs.append((u, os.path.join(CACHE, os.path.basename(u))))
        for var, lvl in SURFACE_FIELDS:
            u = url_for(date_ymd, hh, var, lvl, fff)
            jobs.append((u, os.path.join(CACHE, os.path.basename(u))))
    t0 = time.time()
    nerr = ndone = 0
    njobs = len(jobs)
    print(f"downloading {njobs} grids (mostly cached on re-runs)...", flush=True)
    with ThreadPoolExecutor(max_workers=8) as ex:
        for _, err in ex.map(fetch, jobs):
            nerr += bool(err); ndone += 1
            if ndone % 1000 == 0 or ndone == njobs:
                print(f"  downloading {ndone}/{njobs} ({100*ndone//njobs}%) "
                      f"[{time.time()-t0:.0f}s]", flush=True)
    print(f"downloaded {njobs-nerr}/{njobs} grids in {time.time()-t0:.0f}s", flush=True)

    # ---- station grid indices (from one isobaric grid) ----
    ref = os.path.join(CACHE, fname(date_ymd, hh, "AirTemp", "IsbL-0500", fxx_list[0]))
    iys, ixs = station_indices(ref, lats, lons)
    print("computed station grid indices")

    # static terrain height (SELV) - only at f000, read once, all stations
    ghp = os.path.join(CACHE, fname(date_ymd, hh, "GeopotentialHeight", "Sfc", fxx_list[0]))
    selv_all = read_field(ghp, iys, ixs) if os.path.exists(ghp) else np.full(len(sids), np.nan)

    # ---- extract every grid once (ProcessPool) into per-(fhr,field) arrays ----
    rjobs = []
    for fff in fxx_list:
        for var, role in ISBL_VARS.items():
            for l in LEVELS:
                p = os.path.join(CACHE, fname(date_ymd, hh, var, f"IsbL-{l:04d}", fff))
                rjobs.append((("iso", fff, role, l), p))
        for var, lvl in SURFACE_FIELDS:
            p = os.path.join(CACHE, fname(date_ymd, hh, var, lvl, fff))
            rjobs.append((("sfc", fff, var, lvl), p))
    t0 = time.time()
    iso, sfcd = {}, {}
    nan = np.full(len(sids), np.nan)
    done, total = 0, len(rjobs)
    with ProcessPoolExecutor(max_workers=workers, initializer=_pp_init,
                             initargs=(iys, ixs)) as ex:
        for key, vals in ex.map(_pp_read, rjobs, chunksize=8):
            arr = vals if vals is not None else nan
            (iso if key[0] == "iso" else sfcd)[(key[1], key[2], key[3])] = arr
            done += 1
            if done % 500 == 0 or done == total:
                print(f"  extracting {done}/{total} ({100*done//total}%) "
                      f"[{time.time()-t0:.0f}s]", flush=True)
    print(f"extracted {total} grids in {time.time()-t0:.0f}s ({workers} procs)", flush=True)

    # ---- build + write per-station files ----
    os.makedirs(outdir, exist_ok=True)
    written = 0
    for i, sid in enumerate(sids):
        try:
            # per-station surface pressure series -> fixed (uniform) level set
            psfc_series = []
            for fff in fxx_list:
                v = sfcd[(fff, "Pressure", "Sfc")][i]
                if np.isfinite(v):
                    psfc_series.append(v / 100.0)
            psfc_min = min(psfc_series) if psfc_series else 1015.0
            keep_levels = [l for l in LEVELS if l < psfc_min - 0.1]
            selv = selv_all[i] if np.isfinite(selv_all[i]) else 0.0

            soundings, sfc_rows = [], []
            for fff in fxx_list:
                pts = {role: {l: iso[(fff, role, l)][i] for l in LEVELS}
                       for role in ISBL_VARS.values()}
                psfc_pa = sfcd[(fff, "Pressure", "Sfc")][i]
                psfc = psfc_pa / 100.0 if np.isfinite(psfc_pa) else np.nan
                t2 = sfcd[(fff, "AirTemp", "AGL-2m")][i]
                td2 = sfcd[(fff, "DewPoint", "AGL-2m")][i]
                u10 = sfcd[(fff, "WindU", "AGL-10m")][i]
                v10 = sfcd[(fff, "WindV", "AGL-10m")][i]
                pmsl = sfcd[(fff, "Pressure", "MSL")][i]
                prate = sfcd[(fff, "PrecipRate", "Sfc")][i]

                row = {p: np.nan for p in SFC_PARAMS}
                row["STN"] = stnms[i]
                row["TIME"] = (cycle_dt + pd.Timedelta(hours=fff)).to_pydatetime()
                row["T2MS"] = t2 - 273.15 if np.isfinite(t2) else np.nan
                row["TD2M"] = td2 - 273.15 if np.isfinite(td2) else np.nan
                row["UWND"] = u10
                row["VWND"] = v10
                row["PMSL"] = pmsl / 100.0 if np.isfinite(pmsl) else np.nan
                row["PRES"] = psfc
                row["P01M"] = prate * 10800.0 if np.isfinite(prate) else 0.0
                row["WXTR"] = 1.0 if sfcd[(fff, "Rain-Accum3h", "Sfc")][i] > 1e-6 else 0.0
                row["WXTS"] = 1.0 if sfcd[(fff, "Snow-Accum3h", "Sfc")][i] > 1e-6 else 0.0
                row["WXTZ"] = 1.0 if sfcd[(fff, "FreezingRain-Accum3h", "Sfc")][i] > 1e-6 else 0.0
                row["WXTP"] = 1.0 if sfcd[(fff, "IcePellets-Accum3h", "Sfc")][i] > 1e-6 else 0.0
                # present-weather symbol (WMO code; 999=none). Real PSU files set this;
                # leaving it -9999 makes BUFKIT plot "Error Wx = -9999" on the sounding.
                row["WSYM"] = (66.0 if row["WXTZ"] else 79.0 if row["WXTP"]
                               else 70.0 if row["WXTS"] else 60.0 if row["WXTR"] else 999.0)
                sfc_rows.append(row)

                sfc_scalars = {"psfc": psfc, "t2k": t2, "td2k": td2,
                               "u10": u10, "v10": v10, "hgt": selv}
                snd = build_sounding(pts, fff, cycle_dt, stids[i], stnms[i],
                                     lats[i], lons[i], sfc_scalars, keep_levels)
                soundings.append(snd)

            surface = pd.DataFrame(sfc_rows, columns=["STN", "TIME"] + SFC_PARAMS)
            bf = BufkitFile(snparm=SNPARM, stnprm=STNPRM, soundings=soundings, surface=surface)
            write_buf(bf, os.path.join(outdir, f"rgem_{sid}.buf"),
                      surface_param_order=SFC_PARAMS)
            written += 1
        except Exception as e:
            print(f"  !! {sid} failed: {type(e).__name__}: {e}")
        if (i + 1) % 25 == 0 or (i + 1) == len(sids):
            print(f"  building soundings {i+1}/{len(sids)} stations", flush=True)
    print(f"wrote {written}/{len(sids)} rgem_*.buf to {outdir}", flush=True)
    if written:
        open(statefile, "w").write(cyc_id)   # mark this cycle done

    if publish_repo:
        _publish(outdir, publish_repo)
    return written


def _publish(outdir, repo):
    print(f"publishing to {repo} ...", flush=True)
    from huggingface_hub import HfApi
    api = HfApi()
    api.create_repo(repo, repo_type="dataset", exist_ok=True)
    api.upload_folder(folder_path=outdir, repo_id=repo, repo_type="dataset",
                      allow_patterns=["rgem_*.buf"])
    api.super_squash_history(repo_id=repo, repo_type="dataset")
    print(f"published + squashed -> {repo}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default=os.path.join(ROOT, "out", "rgem"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--fhx", default=",".join(str(f) for f in range(0, 85, 3)))
    ap.add_argument("--publish-repo", default=None)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--statefile", default=None)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    run(a.outdir, [int(x) for x in a.fhx.split(",")], a.limit, a.publish_repo,
        a.workers, a.statefile, a.force)
