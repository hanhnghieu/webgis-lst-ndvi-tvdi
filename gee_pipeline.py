
"""Khai thac Landsat 8/9 tren GEE, tinh NDVI, LST va TVDI theo thang."""

import math
from datetime import date

import ee
import numpy as np
import pandas as pd
from scipy import stats

import config as cfg


L8 = "LANDSAT/LC08/C02/T1_L2"
L9 = "LANDSAT/LC09/C02/T1_L2"

TVDI_CLASSES = [
    "Rất ẩm",
    "Ẩm",
    "Bình thường",
    "Khô hạn",
    "Rất khô hạn",
]


def init_ee():
    """Khoi tao Google Earth Engine."""
    try:
        ee.Initialize(project=cfg.GEE_PROJECT or None)
    except Exception:
        ee.Authenticate()
        ee.Initialize(project=cfg.GEE_PROJECT or None)


def get_roi():
    """Lay khu vuc nghien cuu tu GAUL va gioi han BBOX."""
    fc = (
        ee.FeatureCollection("FAO/GAUL/2015/level1")
        .filter(ee.Filter.eq("ADM0_NAME", "Viet Nam"))
        .filter(
            ee.Filter.inList(
                "ADM1_NAME",
                cfg.GAUL_ADM1_NAMES,
            )
        )
    )

    n = fc.size().getInfo()

    if n != len(cfg.GAUL_ADM1_NAMES):
        names = (
            ee.FeatureCollection("FAO/GAUL/2015/level1")
            .filter(ee.Filter.eq("ADM0_NAME", "Viet Nam"))
            .aggregate_array("ADM1_NAME")
            .getInfo()
        )
        raise RuntimeError(
            f"Chi khop {n} don vi. "
            f"Ten ADM1 trong GAUL: {names}"
        )

    return fc.geometry().dissolve().intersection(
        ee.Geometry.Rectangle(cfg.BBOX),
        1,
    )


def _prep(img):
    """Hieu chinh NDVI, LST va loai pixel co may."""
    qa = img.select("QA_PIXEL")

    # Loai dilated cloud, cirrus, cloud, cloud shadow.
    clear = qa.bitwiseAnd(0b11110).eq(0)

    # Landsat Collection 2 Level 2: chuyen SR sang reflectance.
    red = img.select("SR_B4").multiply(0.0000275).add(-0.2)
    nir = img.select("SR_B5").multiply(0.0000275).add(-0.2)

    denominator = nir.add(red)
    ndvi = (
        nir.subtract(red)
        .divide(denominator)
        .updateMask(denominator.abs().gt(1e-6))
        .rename("NDVI")
    )

    # Chuyen ST_B10 sang Celsius.
    lst = (
        img.select("ST_B10")
        .multiply(0.00341802)
        .add(149.0)
        .subtract(273.15)
        .rename("LST_C")
    )

    out = ndvi.addBands(lst).updateMask(clear)
    return out.updateMask(out.select("NDVI").abs().lte(1))


def _points(img, roi):
    """Lay mau diem theo lo, tranh tai mot phan hoi qua lon."""
    columns = ["lon", "lat", "NDVI", "LST_C"]

    fc = (
        img.sample(
            region=roi,
            scale=cfg.GRID_SCALE_M,
            projection=ee.Projection(cfg.EE_CRS),
            geometries=True,
            dropNulls=False,
            tileScale=4,
        )
        .randomColumn("rnd", 42)
    )

    n = fc.size().getInfo()

    if n == 0:
        return pd.DataFrame(columns=columns, dtype=float)

    batch_size = 2500
    batches = math.ceil(n / batch_size)
    rows = []

    for i in range(batches):
        lower = i / batches
        upper = (i + 1) / batches

        part = fc.filter(
            ee.Filter.And(
                ee.Filter.gte("rnd", lower),
                ee.Filter.lt("rnd", upper),
            )
        )

        features = part.getInfo().get("features", [])

        for feature in features:
            geometry = feature.get("geometry")
            if not geometry:
                continue

            coords = geometry.get("coordinates", [])
            if len(coords) < 2:
                continue

            props = feature.get("properties") or {}

            rows.append(
                (
                    coords[0],
                    coords[1],
                    props.get("NDVI"),
                    props.get("LST_C"),
                )
            )

        print(
            f"  Tai diem: lo {i + 1}/{batches}; "
            f"nhan {len(rows)}/{n} diem",
            flush=True,
        )

    return pd.DataFrame(rows, columns=columns, dtype=float)


def _r2(y, yhat):
    """Tinh he so xac dinh R2."""
    ss_res = float(np.sum((y - yhat) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))

    if ss_tot <= 0:
        return float("nan")

    return 1 - ss_res / ss_tot


def compute_tvdi(df: pd.DataFrame):
    """Tinh TVDI tu bien nhiet do be mat kho va uot."""
    d = df.dropna(subset=["NDVI", "LST_C"]).copy()

    d = d[
        (d["NDVI"] >= 0)
        & (d["NDVI"] <= cfg.NDVI_EDGE_MAX)
    ]

    if d.empty:
        return None, None

    bins = np.arange(
        0,
        cfg.NDVI_EDGE_MAX + 1e-9,
        cfg.NDVI_BIN,
    )

    xs = []
    tmax = []
    tmin = []

    for lo, hi in zip(bins[:-1], bins[1:]):
        values = d.loc[
            (d["NDVI"] >= lo) & (d["NDVI"] < hi),
            "LST_C",
        ]

        if len(values) >= cfg.MIN_PTS_PER_BIN:
            xs.append((lo + hi) / 2)
            tmax.append(values.quantile(0.99))
            tmin.append(values.quantile(0.01))

    if len(xs) < cfg.MIN_BINS:
        return None, None

    xs = np.asarray(xs, dtype=float)
    tmax = np.asarray(tmax, dtype=float)
    tmin = np.asarray(tmin, dtype=float)

    # LST_max = a + b * NDVI
    b, a = np.polyfit(xs, tmax, 1)

    # LST_min = c + d * NDVI
    d_slope, c = np.polyfit(xs, tmin, 1)

    info = {
        "a": float(a),
        "b": float(b),
        "c": float(c),
        "d": float(d_slope),
        "r2_dry": _r2(tmax, a + b * xs),
        "r2_wet": _r2(tmin, c + d_slope * xs),
    }

    lmax = a + b * df["NDVI"]
    lmin = c + d_slope * df["NDVI"]
    width = lmax - lmin

    valid = (
        df["NDVI"].notna()
        & df["LST_C"].notna()
        & (df["NDVI"] > 0)
        & (width >= 1.0)
    )

    tvdi = pd.Series(np.nan, index=df.index, dtype=float)
    tvdi.loc[valid] = (
        (df.loc[valid, "LST_C"] - lmin.loc[valid])
        / width.loc[valid]
    ).clip(0, 1)

    return tvdi, info


def class_distribution(tvdi: pd.Series) -> dict:
    """Thong ke phan tram 5 cap TVDI."""
    cut = pd.cut(
        tvdi.dropna(),
        [0, 0.2, 0.4, 0.6, 0.8, 1.0],
        labels=TVDI_CLASSES,
        include_lowest=True,
    )

    return (
        cut.value_counts(normalize=True)
        .reindex(TVDI_CLASSES)
        .fillna(0)
        .mul(100)
        .round(2)
        .to_dict()
    )


def correlation(df: pd.DataFrame):
    """Tinh tuong quan Pearson giua NDVI va LST."""
    d = df.dropna(subset=["NDVI", "LST_C"])
    d = d[d["NDVI"] > 0]

    if len(d) < 30:
        return None

    result = stats.linregress(d["NDVI"], d["LST_C"])

    return {
        "n": len(d),
        "pearson_r": result.rvalue,
        "slope": result.slope,
        "intercept": result.intercept,
        "r2": result.rvalue ** 2,
        "p_value": result.pvalue,
    }


def process_month(roi, year: int, month: int):
    """Xu ly mot thang, tra ve dict tuong thich voi main.py."""
    start = ee.Date.fromYMD(year, month, 1)
    end = start.advance(1, "month")

    col = (
        ee.ImageCollection(L8)
        .merge(ee.ImageCollection(L9))
        .filterBounds(roi)
        .filterDate(start, end)
        .filter(
            ee.Filter.lt(
                "CLOUD_COVER",
                cfg.MAX_CLOUD,
            )
        )
    )

    # Lay metadata trong mot lan goi getInfo.
    first = ee.Image(col.sort("CLOUD_COVER").first())

    summary = ee.Dictionary(
        {
            "n_images": col.size(),
            "scene": first.toDictionary(
                [
                    "CLOUD_COVER",
                    "SUN_ELEVATION",
                    "DATE_ACQUIRED",
                ]
            ),
            "ref_id": first.get("system:index"),
        }
    ).getInfo()

    n_images = summary["n_images"]

    if not n_images:
        return None

    ref = summary["scene"] or {}

    print(
        f"  {year}-{month:02d}: co {n_images} canh anh, "
        "dang tao anh tong hop...",
        flush=True,
    )

    composite = col.map(_prep).median()
    df = _points(composite, roi)

    if df.empty:
        return {
            "skipped": True,
            "n_images": n_images,
            "valid_ratio": 0.0,
        }

    valid_ratio = float(
        (
            df["NDVI"].notna()
            & df["LST_C"].notna()
        ).mean()
    )

    if valid_ratio < cfg.MIN_VALID_RATIO:
        return {
            "skipped": True,
            "n_images": n_images,
            "valid_ratio": valid_ratio,
        }

    tvdi, edges = compute_tvdi(df)

    df["TVDI"] = (
        tvdi
        if tvdi is not None
        else np.nan
    )

    return {
        "skipped": False,
        "year": year,
        "month": month,
        "df": df,
        "n_images": n_images,
        "valid_ratio": valid_ratio,
        "scene": {
            "id": summary.get("ref_id"),
            "date": ref.get("DATE_ACQUIRED"),
            "cloud": ref.get("CLOUD_COVER"),
            "sun": ref.get("SUN_ELEVATION"),
        },
        "edges": edges,
        "classes": (
            class_distribution(df["TVDI"])
            if edges is not None
            else None
        ),
        "corr": correlation(df),
    }


def months(start_year: int, end_year: int):
    """Chi xu ly cac thang da ket thuc."""
    today = date.today()

    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            if (year, month) < (today.year, today.month):
                yield year, month
