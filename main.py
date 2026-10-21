"""Chạy:  python main.py run [--start-year 2016] [--end-year 2026] [--force]
         python main.py daily [--forecast]      (cập nhật hằng ngày: tháng đang chạy + chốt tháng trước)
         python main.py forecast [--horizon 6]
         python main.py report
         python main.py all"""
import argparse
import os
from datetime import date, timedelta

import pandas as pd

import ai_forecast
import config as cfg
import db
import gee_pipeline as gp


def _append_csv(path, row: dict):
    pd.DataFrame([row]).to_csv(path, mode="a", header=not os.path.exists(path), index=False)


def _ghi_thong_ke(res, y, m):
    """Ghi các file thống kê của tháng (chỉ gọi cho bản chính thức để không bị trùng dòng)."""
    if res["edges"]:
        _append_csv(f"{cfg.OUT_DIR}/tvdi_edges.csv", dict(year=y, month=m, **res["edges"]))
    if res["corr"]:
        _append_csv(f"{cfg.OUT_DIR}/tuong_quan_ndvi_lst.csv", dict(year=y, month=m, **res["corr"]))
    if res["classes"]:
        _append_csv(f"{cfg.OUT_DIR}/tvdi_5_cap.csv", dict(year=y, month=m, **res["classes"]))


def cmd_run(args):
    os.makedirs(cfg.OUT_DIR, exist_ok=True)
    gp.init_ee()
    roi = gp.get_roi()
    with db.connect() as conn:
        db.ensure_state_table(conn)
        for y, m in gp.months(args.start_year, args.end_year):
            # tháng đã có nhưng còn là bản tạm (do lệnh daily ghi) thì tính lại thành bản chính thức
            if not args.force and db.month_exists(conn, y, m) and not db.is_provisional(conn, y, m):
                print(f"{y}-{m:02d}: đã có, bỏ qua")
                continue
            res = gp.process_month(roi, y, m)
            if res is None:
                print(f"{y}-{m:02d}: không có ảnh")
                continue
            _append_csv(f"{cfg.OUT_DIR}/metadata_monthly.csv",
                        dict(year=y, month=m, n_images=res["n_images"], valid_ratio=round(res["valid_ratio"], 3)))
            if res["skipped"]:
                print(f"{y}-{m:02d}: valid_ratio={res['valid_ratio']:.2f} thấp, bỏ qua")
                continue
            n = db.save_month(conn, res)
            db.mark_final(conn, y, m)
            _ghi_thong_ke(res, y, m)
            print(f"{y}-{m:02d}: {n} điểm, {res['n_images']} cảnh, valid_ratio={res['valid_ratio']:.2f}")


def _cap_nhat_thang(conn, roi, y, m, final, force, today) -> bool:
    """Cập nhật một tháng. Chỉ tính lại khi có ảnh mới (hoặc force). Trả về True nếu đã ghi dữ liệu mới."""
    tag = f"{y}-{m:02d}"
    n, last = gp.scan_month(roi, y, m)
    if n == 0:
        print(f"{tag}: chưa có ảnh hợp lệ")
        return False
    st = db.get_state(conn, y, m)
    if not force and st and st["so_canh"] == n and st["anh_moi_nhat"] == last and db.month_exists(conn, y, m):
        print(f"{tag}: không có ảnh mới ({n} cảnh, mới nhất {last:%d/%m/%Y})")
        return False
    res = gp.process_month(roi, y, m)
    if res is None:
        print(f"{tag}: không có ảnh")
        return False
    if res["skipped"]:
        db.set_state(conn, y, m, la_ban_tam=not final, so_canh=n, anh_moi_nhat=last)
        print(f"{tag}: valid_ratio={res['valid_ratio']:.2f} thấp, giữ nguyên dữ liệu cũ")
        return False
    pts = db.save_month(conn, res)
    db.set_state(conn, y, m, la_ban_tam=not final, so_canh=res["n_images"], anh_moi_nhat=last, den_ngay=today)
    if final:
        _append_csv(f"{cfg.OUT_DIR}/metadata_monthly.csv",
                    dict(year=y, month=m, n_images=res["n_images"], valid_ratio=round(res["valid_ratio"], 3)))
        _ghi_thong_ke(res, y, m)
    loai = "chính thức" if final else "tạm"
    print(f"{tag} ({loai}): {pts} điểm, {res['n_images']} cảnh, ảnh mới nhất {last:%d/%m/%Y}, valid_ratio={res['valid_ratio']:.2f}")
    return True


def cmd_daily(args):
    """Chạy mỗi ngày: (1) tháng trước: làm mới đến hết ngày FINAL_AFTER_DAY rồi chốt; (2) tháng hiện tại: bản tạm."""
    os.makedirs(cfg.OUT_DIR, exist_ok=True)
    today = date.today()
    prev = today.replace(day=1) - timedelta(days=1)
    final_prev = today.day >= cfg.FINAL_AFTER_DAY
    print(f"=== daily {today:%d/%m/%Y} ===")
    gp.init_ee()
    roi = gp.get_roi()
    changed = False
    with db.connect() as conn:
        db.ensure_state_table(conn)
        st = db.get_state(conn, prev.year, prev.month)
        if not db.month_exists(conn, prev.year, prev.month) or (st and st["la_ban_tam"]):
            force = final_prev and bool(st and st["la_ban_tam"])      # đến hạn chốt thì tính lại một lần
            changed |= _cap_nhat_thang(conn, roi, prev.year, prev.month, final_prev, force, today)
        changed |= _cap_nhat_thang(conn, roi, today.year, today.month, False, False, today)
    if args.forecast and changed:
        cmd_forecast(args)


def cmd_forecast(args):
    with db.connect() as conn:
        print(f"Đã ghi {ai_forecast.run_forecast(conn, args.horizon)} bản ghi dự báo")


def cmd_report(_):
    os.makedirs(cfg.OUT_DIR, exist_ok=True)
    with db.connect() as conn:
        print("Đã tạo", ai_forecast.ai_commentary(conn, f"{cfg.OUT_DIR}/nhan_xet_ai.md"))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--start-year", type=int, default=cfg.START_YEAR)
    r.add_argument("--end-year", type=int, default=date.today().year)
    r.add_argument("--force", action="store_true", help="tính lại cả tháng đã có")
    d = sub.add_parser("daily")
    d.add_argument("--forecast", action="store_true", help="chạy lại dự báo nếu có dữ liệu mới")
    d.add_argument("--horizon", type=int, default=6)
    f = sub.add_parser("forecast")
    f.add_argument("--horizon", type=int, default=6)
    sub.add_parser("report")
    a = sub.add_parser("all")
    a.add_argument("--start-year", type=int, default=cfg.START_YEAR)
    a.add_argument("--end-year", type=int, default=date.today().year)
    a.add_argument("--force", action="store_true")
    a.add_argument("--horizon", type=int, default=6)
    args = p.parse_args()
    {"run": cmd_run, "daily": cmd_daily, "forecast": cmd_forecast, "report": cmd_report}.get(
        args.cmd, lambda x: (cmd_run(x), cmd_forecast(x), cmd_report(x)))(args)
