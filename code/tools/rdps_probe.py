"""
RDPS (Canadian 10 km regional GEM) helpers, plus a single-station test run.

Holds the MSC datamart conventions (URL and file naming), the download routine,
and build_sounding(), which turns one forecast hour of point values into a
BUFKIT sounding. update_rgem.py imports all of this; run this file directly to
build one station (KBED) as a quick check.

Files live under the dated datamart path:
  https://dd.weather.gc.ca/{YYYYMMDD}/WXO-DD/model_rdps/10km/{HH}/{fff}/
  {YYYYMMDD}T{HH}Z_MSC_RDPS_{VAR}_{LVL}_RLatLon0.09_PT{fff}H.grib2

Each file holds a single GRIB message (one variable, one level, one hour), so
there is no subsetting - every grid is downloaded whole and the station points
are pulled out of it.
"""
from __future__ import annotations

import os
import socket
import sys
import time
import urllib.request
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

warnings.filterwarnings("ignore")

# A stalled connection to the datamart would otherwise block a download thread
# forever (urlretrieve has no timeout of its own).
socket.setdefaulttimeout(60)

import numpy as np
import pandas as pd
import xarray as xr
from metpy.calc import (equivalent_potential_temperature, precipitable_water,
                        relative_humidity_from_dewpoint, wet_bulb_temperature,
                        wind_direction, wind_speed)
from metpy.units import units

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from bufkit_replica.parser import BufkitFile, Sounding, parse  # noqa: E402
from bufkit_replica.writer import write_buf  # noqa: E402
from bufkit_replica.rrfs_bufr import (_convective_indices, _sweat_index,  # noqa: E402
                                      _bulk_richardson, _cloud_frac_rh)

# RDPS lives under the DATED datamart path. The `today/` symlink is EMPTY between
# cycles (and can't see yesterday), which crashes a producer run at that moment, so
# we build the dated path and detect_cycle falls back today -> yesterday.
DDROOT = "https://dd.weather.gc.ca"
GRID = "RLatLon0.09"
CACHE = os.path.join(ROOT, "cache", "rdps")

# isobaric levels (hPa) to pull. RDPS publishes 33 levels (1..1015); we take the
# 28 from 50 hPa down. Above 50 hPa isn't useful in a sounding. T/Td/wind/height exist at all of these; omega only at 4 (filled).
LEVELS = [1015, 1000, 985, 970, 950, 925, 900, 875, 850, 800, 750, 700, 650,
          600, 550, 500, 450, 400, 350, 300, 275, 250, 225, 200, 175, 150, 100, 50]

# isobaric filename-var -> role key
ISBL_VARS = {"AirTemp": "T", "DewPointDepression": "DPD", "WindU": "U",
             "WindV": "V", "GeopotentialHeight": "GH", "VerticalVelocity": "W"}

SNPARM = ["PRES", "TMPC", "TMWC", "DWPC", "THTE", "DRCT", "SKNT", "OMEG", "CFRL", "HGHT"]
STNPRM = ["SHOW", "LIFT", "SWET", "KINX", "LCLP", "PWAT", "TOTL", "CAPE",
          "LCLT", "CINS", "EQLV", "LFCT", "BRCH"]
SFC_PARAMS = ["PMSL", "PRES", "SKTC", "STC1", "SNFL", "WTNS", "P01M", "C01M", "STC2",
              "LCLD", "MCLD", "HCLD", "SNRA", "UWND", "VWND", "R01M", "BFGR", "T2MS",
              "Q2MS", "WXTS", "WXTP", "WXTZ", "WXTR", "USTM", "VSTM", "HLCY", "SLLH",
              "WSYM", "CDBP", "VSBK", "TD2M"]


def fname(date_ymd, hh, var, lvl, fff):
    return f"{date_ymd}T{hh:02d}Z_MSC_RDPS_{var}_{lvl}_{GRID}_PT{fff:03d}H.grib2"


def base_dir(date_ymd, hh):
    return f"{DDROOT}/{date_ymd}/WXO-DD/model_rdps/10km/{hh:02d}"


def url_for(date_ymd, hh, var, lvl, fff):
    return f"{base_dir(date_ymd, hh)}/{fff:03d}/{fname(date_ymd, hh, var, lvl, fff)}"


def _grib_ok(path):
    """A complete GRIB2 message starts with 'GRIB' and ends with '7777'. A dropped
    connection leaves a nonzero-but-truncated file that fails this check."""
    try:
        if os.path.getsize(path) < 8:
            return False
        with open(path, "rb") as fh:
            if fh.read(4) != b"GRIB":
                return False
            fh.seek(-4, 2)
            return fh.read(4) == b"7777"
    except OSError:
        return False


def fetch(args):
    """Download one file to cache (skip if a *valid* copy is present). args=(url, dest).
    Downloads to a temp file and validates the GRIB trailer before renaming into place,
    so an interrupted download can never be cached as a good grid. A pre-existing but
    corrupt cache file is dropped and re-fetched (self-heals truncated downloads)."""
    u, dest = args
    if os.path.exists(dest):
        if _grib_ok(dest):
            return dest, None
        try:
            os.remove(dest)  # truncated/garbage - re-download
        except OSError:
            pass
    tmp = dest + ".part"
    last = "fail"
    for _ in range(3):  # retry timeouts and truncated downloads
        try:
            urllib.request.urlretrieve(u, tmp)
            if _grib_ok(tmp):
                os.replace(tmp, dest)  # atomic
                return dest, None
            last = "TruncatedDownload: GRIB trailer missing"
        except urllib.error.HTTPError as e:  # 404 etc. - retrying won't help
            _drop(tmp)
            return dest, f"HTTPError: {e}"
        except Exception as e:  # timeout, reset connection
            last = f"{type(e).__name__}: {e}"
        _drop(tmp)
    return dest, last


def _drop(path):
    try:
        os.remove(path)
    except OSError:
        pass


def read_point(path, iy, ix):
    """Scalar value at (iy,ix) from a single-message GRIB file."""
    ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
    v = list(ds.data_vars)[0]
    val = float(ds[v].values[iy, ix])
    ds.close()
    return val


def grid_index(path, lat0, lon0):
    """Nearest (iy,ix) on the RDPS grid for a station; grid shared by all files."""
    ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
    lat = ds.latitude.values
    lon = ds.longitude.values
    lon0n = lon0 % 360
    d2 = (lat - lat0) ** 2 + (((lon - lon0n + 180) % 360) - 180) ** 2
    iy, ix = np.unravel_index(np.argmin(d2), lat.shape)
    glat, glon = float(lat[iy, ix]), float(lon[iy, ix])
    ds.close()
    return int(iy), int(ix), glat, glon


def detect_cycle():
    """Newest RDPS cycle (with f084 posted) under the DATED datamart path. RDPS runs
    00/06/12/18 Z; the `today/` symlink is empty between cycles and can't see
    yesterday, so check today then yesterday (UTC) and require the last forecast hour
    to exist so we never grab a half-posted cycle."""
    import re
    from datetime import datetime, timedelta, timezone
    for dayoff in (0, 1):
        d = (datetime.now(timezone.utc) - timedelta(days=dayoff)).strftime("%Y%m%d")
        try:
            html = urllib.request.urlopen(
                f"{DDROOT}/{d}/WXO-DD/model_rdps/10km/", timeout=30).read().decode("latin1")
        except Exception:
            continue
        for hh in sorted((int(m) for m in re.findall(r'href="(\d\d)/"', html)), reverse=True):
            tail = url_for(d, hh, "AirTemp", "IsbL-0500", 84)
            try:
                if urllib.request.urlopen(
                        urllib.request.Request(tail, method="HEAD"), timeout=30).status == 200:
                    return d, hh
            except Exception:
                continue
    raise RuntimeError("no complete RDPS cycle found (today or yesterday)")


def build_sounding(pts, fff, cycle_dt, stid, stnm, lat0, lon0, sfc, keep_levels):
    """pts: dict role->{lvl: value}; sfc: surface scalars; keep_levels: a FIXED
    isobaric level set (hPa), above the lowest surface pressure across all fhrs,
    so every sounding has an IDENTICAL level count - BUFKIT requires uniform
    levels (varying counts -> run-time error 9, subscript out of range). Anchored
    at the surface (first level = surface PRES=psfc, HGHT=SELV) like real files."""
    kl = keep_levels
    Piso = np.array(kl, dtype=float)
    Tk = np.array([pts["T"][l] for l in kl])
    dpd = np.array([pts["DPD"][l] for l in kl])
    U = np.array([pts["U"][l] for l in kl])
    V = np.array([pts["V"][l] for l in kl])
    GH = np.array([pts["GH"][l] for l in kl])
    W = np.array([pts["W"][l] for l in kl])
    Tdk = Tk - dpd                      # dewpoint depression -> dewpoint (K)

    psfc = sfc["psfc"]                   # hPa
    selv = sfc["hgt"]                    # model surface geopotential height (m) = SELV
    have_sfc = all(np.isfinite(x) for x in (psfc, sfc["t2k"], sfc["td2k"], selv))
    if have_sfc:                         # all kl are above ground by construction
        P_a = np.concatenate([[psfc], Piso])
        Tk_a = np.concatenate([[sfc["t2k"]], Tk])
        Tdk_a = np.concatenate([[sfc["td2k"]], Tdk])
        U_a = np.concatenate([[sfc["u10"]], U])
        V_a = np.concatenate([[sfc["v10"]], V])
        GH_a = np.concatenate([[selv], GH])
        W_a = np.concatenate([[0.0], W])
    else:                               # fallback: isobaric only, no surface anchor
        P_a, Tk_a, Tdk_a = Piso, Tk, Tdk
        U_a, V_a, GH_a, W_a = U, V, GH, W
        selv = selv if np.isfinite(selv) else 0.0

    Tdk_a = np.minimum(Tdk_a, Tk_a)     # enforce Td <= T invariant
    Pq = P_a * units.hPa
    Tkq = Tk_a * units.kelvin
    Tdkq = Tdk_a * units.kelvin
    uq = U_a * units("m/s")
    vq = V_a * units("m/s")

    Tc = Tkq.to("degC").m
    Tdc = Tdkq.to("degC").m
    drct = wind_direction(uq, vq).m
    sknt = wind_speed(uq, vq).to("knots").m
    thte = equivalent_potential_temperature(Pq, Tkq, Tdkq).m
    tmwc = wet_bulb_temperature(Pq, Tkq, Tdkq).to("degC").m
    rh = relative_humidity_from_dewpoint(Tkq, Tdkq).to("dimensionless").m * 100.0

    # RDPS only publishes vertical velocity at 850/700/500/250 hPa. Zero-filling
    # the other levels leaves spikes, so interpolate linearly in pressure between
    # the published levels, with omega = 0 at the surface and at the column top.
    # Every level ends up filled, which BUFKIT needs (no missing OMEG).
    finite = np.isfinite(W_a)
    xp = np.concatenate([P_a[finite], [P_a[-1]]])   # known omega levels + a 0 top anchor
    fp = np.concatenate([W_a[finite], [0.0]])
    if len(xp) >= 2:
        order = np.argsort(xp)
        omeg = np.interp(P_a, xp[order], fp[order])
    else:
        omeg = np.zeros_like(P_a)

    levels = pd.DataFrame({
        "PRES": Pq.m, "TMPC": Tc, "TMWC": tmwc, "DWPC": Tdc, "THTE": thte,
        "DRCT": drct, "SKNT": sknt, "OMEG": omeg,
        "CFRL": _cloud_frac_rh(rh), "HGHT": GH_a,
    })[SNPARM]

    # convective indices - reuse the RRFS decoder's MetPy routines so BUFKIT shows
    # real CAPE/LIFT/SHOW/etc. (paired LFCT/EQLV dropped together when no LFC).
    P_hPa = Pq.m
    um, vm = uq.to("m/s").m, vq.to("m/s").m
    derived = _convective_indices(P_hPa, Tc, Tdc)
    derived["SWET"] = _sweat_index(P_hPa, Tdc, um, vm, derived.get("TOTL", np.nan))
    derived["BRCH"] = _bulk_richardson(derived.get("CAPE"), um, vm, GH_a)

    valid = (cycle_dt + pd.Timedelta(hours=fff)).to_pydatetime()
    return Sounding(stid=stid, stnm=stnm, valid_time=valid, slat=lat0, slon=lon0,
                    selv=int(round(selv)), derived=derived, levels=levels)


def run(stid, lat0, lon0, selv, stnm, fxx_list, outdir):
    os.makedirs(CACHE, exist_ok=True)
    date_ymd, hh = detect_cycle()
    cycle_dt = pd.Timestamp(datetime.strptime(f"{date_ymd}{hh:02d}", "%Y%m%d%H"))
    print(f"RDPS cycle {date_ymd} {hh:02d}Z  station {stid} ({lat0},{lon0})  fxx={list(fxx_list)}")

    # build the full download list (isobaric + surface) for all fhrs
    jobs = []
    for fff in fxx_list:
        for var in ISBL_VARS:
            for l in LEVELS:
                u = url_for(date_ymd, hh, var, f"IsbL-{l:04d}", fff)
                jobs.append((u, os.path.join(CACHE, os.path.basename(u))))
        for var, lvl in [("AirTemp", "AGL-2m"), ("DewPoint", "AGL-2m"),
                         ("WindU", "AGL-10m"), ("WindV", "AGL-10m"),
                         ("Pressure", "MSL"), ("Pressure", "Sfc"),
                         ("GeopotentialHeight", "Sfc"), ("PrecipRate", "Sfc"),
                         ("Rain-Accum3h", "Sfc"), ("Snow-Accum3h", "Sfc"),
                         ("FreezingRain-Accum3h", "Sfc"), ("IcePellets-Accum3h", "Sfc")]:
            u = url_for(date_ymd, hh, var, lvl, fff)
            jobs.append((u, os.path.join(CACHE, os.path.basename(u))))

    t0 = time.time()
    n_err = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for dest, err in ex.map(fetch, jobs):
            if err:
                n_err += 1
    print(f"downloaded {len(jobs)-n_err}/{len(jobs)} files in {time.time()-t0:.0f}s "
          f"({n_err} errors)")

    # nearest grid index from one isobaric file
    ref = os.path.join(CACHE, fname(date_ymd, hh, "AirTemp", "IsbL-0500", fxx_list[0]))
    iy, ix, glat, glon = grid_index(ref, lat0, lon0)
    print(f"nearest grid point: {glat:.3f},{glon:.3f}")

    # uniform level set: isobaric levels above the LOWEST surface pressure across
    # all fhrs, so every sounding has an identical count (else BUFKIT error 9)
    psfc_all = []
    for fff in fxx_list:
        p = os.path.join(CACHE, fname(date_ymd, hh, "Pressure", "Sfc", fff))
        v = read_point(p, iy, ix) if os.path.exists(p) else np.nan
        if np.isfinite(v):
            psfc_all.append(v / 100.0)
    psfc_min = min(psfc_all) if psfc_all else 1015.0
    keep_levels = [l for l in LEVELS if l < psfc_min - 0.1]
    print(f"psfc_min={psfc_min:.1f} hPa -> {len(keep_levels)} isobaric + surface "
          f"= {len(keep_levels)+1} levels (uniform across all {len(fxx_list)} times)")

    # SELV (model terrain height) is static -> MSC posts GeopotentialHeight_Sfc only
    # at f000. Read once and reuse, else later hours lose the surface anchor.
    ghp = os.path.join(CACHE, fname(date_ymd, hh, "GeopotentialHeight", "Sfc", fxx_list[0]))
    selv_const = read_point(ghp, iy, ix) if os.path.exists(ghp) else np.nan
    if not np.isfinite(selv_const):
        selv_const = float(selv)        # fall back to caller-provided elevation
    print(f"SELV (model terrain) = {selv_const:.0f} m")

    soundings, sfc_rows = [], []
    for fff in fxx_list:
        # surface
        def sfc(var, lvl):
            p = os.path.join(CACHE, fname(date_ymd, hh, var, lvl, fff))
            return read_point(p, iy, ix) if os.path.exists(p) else np.nan
        psfc_pa = sfc("Pressure", "Sfc")
        psfc = psfc_pa / 100.0 if np.isfinite(psfc_pa) else np.nan
        pmsl = sfc("Pressure", "MSL")
        t2 = sfc("AirTemp", "AGL-2m")
        td2 = sfc("DewPoint", "AGL-2m")
        u10 = sfc("WindU", "AGL-10m")
        v10 = sfc("WindV", "AGL-10m")
        sfc_hgt = selv_const            # static terrain height, read once above
        row = {p: np.nan for p in SFC_PARAMS}
        row["STN"] = stnm
        row["TIME"] = (cycle_dt + pd.Timedelta(hours=fff)).to_pydatetime()
        row["T2MS"] = t2 - 273.15 if np.isfinite(t2) else np.nan
        row["TD2M"] = td2 - 273.15 if np.isfinite(td2) else np.nan
        row["UWND"] = u10
        row["VWND"] = v10
        row["PMSL"] = pmsl / 100.0 if np.isfinite(pmsl) else np.nan
        row["PRES"] = psfc
        # precip: P01M = total precip over the 3-h step (rate kg/m2/s -> mm); WXT*
        # type flags from whichever per-type 3-h accumulation is non-zero. These
        # fields are absent at f000 (analysis) -> 0, which is correct (no precip).
        prate = sfc("PrecipRate", "Sfc")
        row["P01M"] = prate * 10800.0 if np.isfinite(prate) else 0.0

        def wxt(var):
            v = sfc(var, "Sfc")
            return 1.0 if (np.isfinite(v) and v > 1e-6) else 0.0
        row["WXTR"] = wxt("Rain-Accum3h")
        row["WXTS"] = wxt("Snow-Accum3h")
        row["WXTZ"] = wxt("FreezingRain-Accum3h")
        row["WXTP"] = wxt("IcePellets-Accum3h")
        # present-weather symbol (WMO code; 999=none) - else BUFKIT "Error Wx = -9999"
        row["WSYM"] = (66.0 if row["WXTZ"] else 79.0 if row["WXTP"]
                       else 70.0 if row["WXTS"] else 60.0 if row["WXTR"] else 999.0)
        sfc_rows.append(row)

        # isobaric column
        pts = {role: {} for role in ISBL_VARS.values()}
        for var, role in ISBL_VARS.items():
            for l in LEVELS:
                p = os.path.join(CACHE, fname(date_ymd, hh, var, f"IsbL-{l:04d}", fff))
                pts[role][l] = read_point(p, iy, ix) if os.path.exists(p) else np.nan
        sfc_scalars = {"psfc": psfc, "t2k": t2, "td2k": td2,
                       "u10": u10, "v10": v10, "hgt": sfc_hgt}
        snd = build_sounding(pts, fff, cycle_dt, stid, stnm, lat0, lon0,
                             sfc_scalars, keep_levels)
        soundings.append(snd)
        print(f"  f{fff:03d}: {len(snd.levels)} levels, "
              f"sfc T2M={row['T2MS']:.1f}C Td2M={row['TD2M']:.1f}C")

    surface = pd.DataFrame(sfc_rows, columns=["STN", "TIME"] + SFC_PARAMS)
    bf = BufkitFile(snparm=SNPARM, stnprm=STNPRM, soundings=soundings, surface=surface)

    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, f"rgem_{stid.lower()}.buf")  # BUFKIT reserves "RDPS" -> use RGEM
    write_buf(bf, out, surface_param_order=SFC_PARAMS)
    print(f"WROTE {out}  ({os.path.getsize(out)//1024} KB)")

    # round-trip sanity
    bf2 = parse(out)
    print(f"round-trip OK: {len(bf2.soundings)} soundings, "
          f"{len(bf2.soundings[0].levels)} levels, surface rows={len(bf2.surface)}")
    return out


if __name__ == "__main__":
    # test station: KBED (Hanscom Field, Bedford MA)
    run(stid="KBED", lat0=42.47, lon0=-71.29, selv=41, stnm=744900,
        fxx_list=list(range(0, 85, 3)), outdir=os.path.join(ROOT, "out"))  # 3-hrly to 84h
