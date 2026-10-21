"""WebGIS FastAPI. Chạy từ thư mục lst_pipeline:  uvicorn webgis.app:app --reload"""
import os
import time
from contextlib import contextmanager

import psycopg2
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from psycopg2.pool import ThreadedConnectionPool

import config as cfg

D, V, P = cfg.COL_DIEM, cfg.COL_VUNG, cfg.COL_DUBAO
T, TV, TP = cfg.TBL_DIEM, cfg.TBL_VUNG, cfg.TBL_DUBAO
COL = {"lst": D["lst"], "ndvi": D["ndvi"], "tvdi": D["tvdi"]}
NG, GEOM, SRID = D["ngay"], D["geom"], cfg.GEOM_SRID
TEN_HUYEN, TEN_TINH = cfg.ident(os.getenv("COL_TEN_HUYEN", "ten_huyen")), cfg.ident(os.getenv("COL_TEN_TINH", "ten_tinh"))
TOL = 1e-5 if SRID == 4326 else 1.0           # dung sai khi ghép cùng một điểm lưới

pool = ThreadedConnectionPool(1, 8, host=os.getenv("PG_HOST", "localhost"), port=os.getenv("PG_PORT", "5432"),
                              dbname=os.getenv("PG_DB"), user=os.getenv("PG_USER"), password=os.getenv("PG_PASSWORD"))
app = FastAPI(title="WebGIS LST – NDVI – TVDI")
app.add_middleware(GZipMiddleware, minimum_size=1000)
META_TTL = 600                                # giây; sau thời gian này /api/meta tự đọc lại CSDL, không cần khởi động lại
_cache = {}


@contextmanager
def cur():
    c = pool.getconn()
    try:
        with c.cursor() as k:
            yield k
    except Exception:
        c.rollback()
        raise
    finally:
        pool.putconn(c)


def chi(chi_so: str) -> str:
    if chi_so not in COL:
        raise HTTPException(400, "chi_so phải là lst, ndvi hoặc tvdi")
    return COL[chi_so]


def tfilter(nam: int, thang: int):
    """thang: 0 = cả năm, 1–12 = tháng, 13 = mùa khô (T1–4, T11–12)."""
    y = f"EXTRACT(YEAR FROM {NG})=%s"
    if thang == 0:
        return y, (nam,)
    if thang == 13:
        return f"{y} AND EXTRACT(MONTH FROM {NG}) IN (1,2,3,4,11,12)", (nam,)
    return f"{y} AND EXTRACT(MONTH FROM {NG})=%s", (nam, thang)


@app.get("/api/meta")
def meta():
    if "meta" not in _cache or time.time() - _cache["meta_t"] > META_TTL:
        with cur() as k:
            k.execute(f"SELECT DISTINCT EXTRACT(YEAR FROM {NG})::int FROM {T} ORDER BY 1")
            years = [r[0] for r in k.fetchall()]
            ranges = {}
            for key, col in COL.items():
                k.execute(f"SELECT percentile_cont(ARRAY[0.02,0.98]) WITHIN GROUP (ORDER BY {col}) FROM {T}")
                ranges[key] = k.fetchone()[0]
        tam = []                               # các tháng đang là bản tạm (do lệnh main.py daily ghi)
        try:
            with cur() as k:
                k.execute(f"SELECT EXTRACT(YEAR FROM s.thang)::int, EXTRACT(MONTH FROM s.thang)::int, COALESCE(s.anh_moi_nhat, s.den_ngay) "
                          f"FROM {cfg.TBL_TRANGTHAI} s WHERE s.la_ban_tam AND EXISTS (SELECT 1 FROM {T} d WHERE d.{NG}=s.thang) ORDER BY 1,2")
                tam = [{"nam": a, "thang": b, "den": c.isoformat() if c else None} for a, b, c in k.fetchall()]
        except psycopg2.Error:
            pass                               # chưa có bảng trạng thái
        _cache["meta"] = {"years": years, "ranges": ranges, "cell_m": cfg.GRID_SCALE_M, "tam": tam}
        _cache["meta_t"] = time.time()
    return _cache["meta"]


@app.get("/api/grid")
def grid(chi_so: str, nam: int, thang: int = 0):
    col, (w, p) = chi(chi_so), tfilter(nam, thang)
    with cur() as k:
        k.execute(
            f"SELECT ROUND(ST_X(g)::numeric,5), ROUND(ST_Y(g)::numeric,5), AVG(v) FROM "
            f"(SELECT ST_Transform({GEOM},4326) g, {col} v FROM {T} WHERE {w}) t "
            f"GROUP BY 1,2 HAVING AVG(v) IS NOT NULL", p)
        pts = [[float(a), float(b), round(float(c), 4)] for a, b, c in k.fetchall()]
    return {"pts": pts}


@app.get("/api/khuvuc")
def khuvuc(chi_so: str, nam: int, thang: int = 0):
    col, (w, p) = chi(chi_so), tfilter(nam, thang)
    with cur() as k:
        k.execute(
            f"SELECT r.{TEN_HUYEN}, r.{TEN_TINH}, AVG(d.{col}), COUNT(*) FROM {T} d JOIN {TV} r ON r.{V['id']}=d.{D['vung']} "
            f"WHERE {w.replace(NG, 'd.' + NG)} GROUP BY 1,2 ORDER BY 3 DESC", p)
        return [{"huyen": a, "tinh": b, "tb": round(float(c), 3), "n": n} for a, b, c, n in k.fetchall() if c is not None]


@app.get("/api/series")
def series(lon: float | None = Query(None), lat: float | None = Query(None)):
    """Chuỗi tháng của một điểm lưới (hoặc trung bình toàn vùng) + dự báo mới nhất."""
    vung = None
    out = {"snap": None, "hist": {}, "pred": {}}
    sel = f"{NG}, {COL['lst']}, {COL['ndvi']}, {COL['tvdi']}"
    with cur() as k:
        if lon is None:
            k.execute(f"SELECT date_trunc('month',{NG})::date, AVG({COL['lst']}), AVG({COL['ndvi']}), AVG({COL['tvdi']}) "
                      f"FROM {T} GROUP BY 1 ORDER BY 1")
        else:
            pt = f"ST_Transform(ST_SetSRID(ST_MakePoint(%s,%s),4326),{SRID})"
            k.execute(
                f"WITH p AS (SELECT {GEOM} g, {D['vung']} v, ST_Distance(ST_Transform({GEOM},4326)::geography, "
                f"ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography) dist FROM {T} ORDER BY {GEOM} <-> {pt} LIMIT 1) "
                f"SELECT {sel}, p.v, p.dist, ST_X(ST_Transform(p.g,4326)), ST_Y(ST_Transform(p.g,4326)) "
                f"FROM {T} t JOIN p ON ST_DWithin(t.{GEOM}, p.g, {TOL}) ORDER BY {NG}", (lon, lat, lon, lat))
        rows = k.fetchall()
    if not rows:
        raise HTTPException(404, "Không có dữ liệu")
    if lon is not None:
        if rows[0][5] > cfg.GRID_SCALE_M * 2:
            raise HTTPException(404, "Điểm nằm ngoài vùng dữ liệu")
        vung, out["snap"] = rows[0][4], [rows[0][6], rows[0][7]]
    for i, key in enumerate(("LST", "NDVI", "TVDI"), 1):
        out["hist"][key] = [[r[0].strftime("%Y-%m"), None if r[i] is None else round(float(r[i]), 3)] for r in rows]
    try:
        with cur() as k:
            f = f"{P['vung']}=%s AND " if vung is not None else ""
            k.execute(
                f"SELECT {P['chi_so']}, {P['ngay']}, AVG({P['gia_tri']}), AVG({P['duoi']}), AVG({P['tren']}) FROM {TP} "
                f"WHERE {f}{P['ref']}=(SELECT MAX({P['ref']}) FROM {TP}) GROUP BY 1,2 ORDER BY 2",
                (vung,) if vung is not None else ())
            for c, d, v, lo, hi in k.fetchall():
                out["pred"].setdefault(c.upper(), []).append([d.strftime("%Y-%m"), round(float(v), 3), round(float(lo), 3), round(float(hi), 3)])
    except psycopg2.Error:
        pass                                   # chưa có bảng/dữ liệu dự báo
    return out


@app.get("/api/ranhgioi")
def ranhgioi():
    if "rg" not in _cache:
        with cur() as k:
            k.execute(
                f"SELECT json_build_object('type','FeatureCollection','features',COALESCE(json_agg(json_build_object("
                f"'type','Feature','properties',json_build_object('ten',{TEN_HUYEN},'tinh',{TEN_TINH}),"
                f"'geometry',ST_AsGeoJSON(ST_SimplifyPreserveTopology(ST_Transform({V['geom']},4326),0.002))::json)),'[]'::json)) FROM {TV}")
            _cache["rg"] = k.fetchone()[0]
    return _cache["rg"]


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True), name="static")
