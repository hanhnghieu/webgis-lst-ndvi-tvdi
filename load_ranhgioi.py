"""Nạp ranh giới huyện (FAO GAUL level 2) của TP.HCM, Bình Dương, BR-VT vào bảng ranh_gioi_khong_gian.
Chạy một lần sau khi tạo bảng:  python load_ranhgioi.py"""
import ee

import config as cfg
import db
import gee_pipeline as gp

V = cfg.COL_VUNG


def main():
    gp.init_ee()
    roi = gp.get_roi()
    fc = (ee.FeatureCollection("FAO/GAUL/2015/level2")
          .filter(ee.Filter.eq("ADM0_NAME", "Viet Nam"))
          .filter(ee.Filter.inList("ADM1_NAME", cfg.GAUL_ADM1_NAMES))
          .filterBounds(roi)
          .map(lambda f: f.setGeometry(f.geometry().intersection(roi, 100).simplify(100))))
    feats = fc.getInfo()["features"]
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(f"DELETE FROM {cfg.TBL_VUNG}") if input("Xóa dữ liệu cũ của bảng ranh giới? (y/N) ").lower() == "y" else None
        for f in feats:
            p = f["properties"]
            cur.execute(
                f"INSERT INTO {cfg.TBL_VUNG} (ten_tinh, ten_huyen, loai_lop_phu, {V['geom']}) VALUES (%s,%s,%s,"
                f"ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)),3)))",
                (p.get("ADM1_NAME"), p.get("ADM2_NAME"), "huyen_cu_GAUL2015", __import__("json").dumps(f["geometry"])))
        conn.commit()
    print(f"Đã nạp {len(feats)} huyện")


if __name__ == "__main__":
    main()
