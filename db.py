"""Ghi/đọc PostgreSQL/PostGIS theo cấu trúc Bảng 6.1."""
from datetime import date

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

import config as cfg

D, A, V, P = cfg.COL_DIEM, cfg.COL_ANH, cfg.COL_VUNG, cfg.COL_DUBAO


def connect():
    import os
    return psycopg2.connect(host=os.getenv("PG_HOST", "localhost"), port=os.getenv("PG_PORT", "5432"),
                            dbname=os.getenv("PG_DB"), user=os.getenv("PG_USER"),
                            password=os.getenv("PG_PASSWORD"))


def ensure_state_table(conn):
    """Bảng phụ ghi trạng thái từng tháng: bản tạm (đang cập nhật) hay bản chính thức. Không đụng tới bảng chính."""
    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE IF NOT EXISTS {cfg.TBL_TRANGTHAI} ("
                    "thang date PRIMARY KEY, la_ban_tam boolean NOT NULL DEFAULT true, so_canh integer, "
                    "anh_moi_nhat date, den_ngay date, cap_nhat_luc timestamptz NOT NULL DEFAULT now())")
    conn.commit()


def get_state(conn, y: int, m: int):
    with conn.cursor() as cur:
        cur.execute(f"SELECT la_ban_tam, so_canh, anh_moi_nhat FROM {cfg.TBL_TRANGTHAI} WHERE thang=%s", (date(y, m, 1),))
        r = cur.fetchone()
    return None if r is None else dict(la_ban_tam=r[0], so_canh=r[1], anh_moi_nhat=r[2])


def is_provisional(conn, y: int, m: int) -> bool:
    st = get_state(conn, y, m)
    return bool(st and st["la_ban_tam"])


def set_state(conn, y: int, m: int, la_ban_tam: bool, so_canh: int, anh_moi_nhat, den_ngay=None):
    t = cfg.TBL_TRANGTHAI
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {t} (thang, la_ban_tam, so_canh, anh_moi_nhat, den_ngay) VALUES (%s,%s,%s,%s,%s) "
            f"ON CONFLICT (thang) DO UPDATE SET la_ban_tam=EXCLUDED.la_ban_tam, so_canh=EXCLUDED.so_canh, "
            f"anh_moi_nhat=EXCLUDED.anh_moi_nhat, den_ngay=COALESCE(EXCLUDED.den_ngay, {t}.den_ngay), cap_nhat_luc=now()",
            (date(y, m, 1), la_ban_tam, so_canh, anh_moi_nhat, den_ngay))
    conn.commit()


def mark_final(conn, y: int, m: int):
    with conn.cursor() as cur:
        cur.execute(f"UPDATE {cfg.TBL_TRANGTHAI} SET la_ban_tam=false, cap_nhat_luc=now() WHERE thang=%s", (date(y, m, 1),))
    conn.commit()


def month_exists(conn, y: int, m: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(f"SELECT 1 FROM {cfg.TBL_DIEM} WHERE {D['ngay']} = %s LIMIT 1", (date(y, m, 1),))
        return cur.fetchone() is not None


def save_month(conn, res: dict):
    """Ghi thông tin cảnh ảnh và các điểm lưới của một tháng (xóa rồi ghi lại nên chạy lại không bị trùng)."""
    s, df = res["scene"], res["df"]
    day = date(res["year"], res["month"], 1)
    df = df.dropna(subset=["NDVI", "LST_C"])
    with conn.cursor() as cur:
        # Cảnh ảnh tham chiếu của tháng (ít mây nhất); điểm lưới lấy từ ảnh tổng hợp trung vị cả tháng.
        cur.execute(
            f"INSERT INTO {cfg.TBL_ANH} ({A['id']},{A['ngay']},{A['may']},{A['sun']}) "
            f"VALUES (%s,%s,%s,%s) ON CONFLICT ({A['id']}) DO NOTHING",
            (s["id"], s["date"], s["cloud"], s["sun"]))
        cur.execute(f"DELETE FROM {cfg.TBL_DIEM} WHERE {D['ngay']} = %s", (day,))
        pt = f"ST_Transform(ST_SetSRID(ST_MakePoint(%s,%s),4326),{cfg.GEOM_SRID})"
        template = (f"(%s,(SELECT r.{V['id']} FROM {cfg.TBL_VUNG} r "
                    f"WHERE ST_Intersects(r.{V['geom']},{pt}) LIMIT 1),%s,%s,%s,%s,{pt})")
        rows = [(s["id"], r.lon, r.lat, day, float(r.LST_C), float(r.NDVI),
                 None if pd.isna(r.TVDI) else float(r.TVDI), r.lon, r.lat)
                for r in df.itertuples()]
        execute_values(
            cur,
            f"INSERT INTO {cfg.TBL_DIEM} ({D['scene']},{D['vung']},{D['ngay']},{D['lst']},{D['ndvi']},{D['tvdi']},{D['geom']}) VALUES %s",
            rows, template=template, page_size=500)
    conn.commit()
    return len(rows)


def read_monthly(conn) -> pd.DataFrame:
    """Trung bình theo khu vực và tháng, dùng cho dự báo và báo cáo (bỏ qua tháng tạm chưa chốt)."""
    ensure_state_table(conn)
    q = (f"SELECT {D['vung']}, date_trunc('month',{D['ngay']})::date, AVG({D['lst']}), AVG({D['ndvi']}), AVG({D['tvdi']}) "
         f"FROM {cfg.TBL_DIEM} WHERE {D['vung']} IS NOT NULL AND NOT EXISTS "
         f"(SELECT 1 FROM {cfg.TBL_TRANGTHAI} s WHERE s.thang = {D['ngay']} AND s.la_ban_tam) GROUP BY 1,2 ORDER BY 1,2")
    with conn.cursor() as cur:
        cur.execute(q)
        df = pd.DataFrame(cur.fetchall(), columns=["ma_khu_vuc", "thang", "LST", "NDVI", "TVDI"])
    df["thang"] = pd.to_datetime(df["thang"])
    return df.astype({"LST": float, "NDVI": float, "TVDI": float})


def save_forecast(conn, rows: list, mo_hinh: str, ngay_ref: date):
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {cfg.TBL_DUBAO} WHERE {P['mo_hinh']}=%s AND {P['ref']}=%s", (mo_hinh, ngay_ref))
        execute_values(
            cur,
            f"INSERT INTO {cfg.TBL_DUBAO} ({P['vung']},{P['chi_so']},{P['ngay']},{P['gia_tri']},{P['mo_hinh']},"
            f"{P['duoi']},{P['tren']},{P['kich_ban']},{P['ref']}) VALUES %s", rows)
    conn.commit()