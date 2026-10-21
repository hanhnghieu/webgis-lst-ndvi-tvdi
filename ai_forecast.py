"""AI: (1) mô hình ML dự báo chuỗi tháng, (2) Claude viết nhận xét tiếng Việt."""
import os
from datetime import date

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression

import db

MO_HINH = "HoiQuy_XuThe_MuaVu"
KICH_BAN = "co_so"
MIN_MONTHS = 24            # cần ít nhất 2 năm để tách được tính mùa vụ


def _features(t: np.ndarray) -> np.ndarray:
    ang = 2 * np.pi * (t % 12) / 12
    return np.column_stack([t, np.sin(ang), np.cos(ang)])


def forecast_series(thang: pd.Series, y: pd.Series, horizon: int):
    """Hồi quy xu thế + mùa vụ (sin/cos theo tháng). Khoảng tin cậy ≈ ±1,96 × độ lệch chuẩn phần dư."""
    d = pd.DataFrame({"thang": thang, "y": y}).dropna()
    if len(d) < MIN_MONTHS:
        return []
    t = (d.thang.dt.year * 12 + d.thang.dt.month).to_numpy()
    model = LinearRegression().fit(_features(t), d.y.to_numpy())
    resid = d.y.to_numpy() - model.predict(_features(t))
    sd = float(np.std(resid, ddof=3))
    out = []
    last = d.thang.max()
    for h in range(1, horizon + 1):
        nxt = last + pd.DateOffset(months=h)
        tt = np.array([nxt.year * 12 + nxt.month])
        yhat = float(model.predict(_features(tt))[0])
        out.append((nxt.date().replace(day=1), yhat, yhat - 1.96 * sd, yhat + 1.96 * sd))
    return out


def run_forecast(conn, horizon: int = 6) -> int:
    monthly = db.read_monthly(conn)
    today = date.today()
    rows = []
    for vung, g in monthly.groupby("ma_khu_vuc"):
        for chi_so in ("LST", "NDVI", "TVDI"):
            for ngay, v, lo, hi in forecast_series(g.thang, g[chi_so], horizon):
                if chi_so in ("NDVI", "TVDI"):
                    lo, hi = max(lo, -1 if chi_so == "NDVI" else 0), min(hi, 1)
                    v = min(max(v, lo), hi)
                rows.append((vung, chi_so, ngay, v, MO_HINH, lo, hi, KICH_BAN, today))
    if rows:
        db.save_forecast(conn, rows, MO_HINH, today)
    return len(rows)


def ai_commentary(conn, out_path: str):
    """Gửi bảng thống kê tổng hợp (không gửi dữ liệu thô) cho Claude để viết nhận xét."""
    import anthropic
    monthly = db.read_monthly(conn)
    if monthly.empty:
        raise RuntimeError("CSDL chưa có dữ liệu.")
    latest = monthly.thang.max()
    cur = monthly[monthly.thang == latest].set_index("ma_khu_vuc")
    prev = monthly[monthly.thang == latest - pd.DateOffset(years=1)].set_index("ma_khu_vuc")
    delta = (cur[["LST", "NDVI", "TVDI"]] - prev[["LST", "NDVI", "TVDI"]]).add_suffix("_chenh_so_voi_cung_ky_nam_truoc")
    table = cur.join(delta).round(3).to_string()
    prompt = (
        f"Dưới đây là thống kê theo khu vực (ma_khu_vuc) cho tháng {latest:%m/%Y} tại TP. Hồ Chí Minh sau sáp nhập "
        "(LST: °C, NDVI, TVDI từ Landsat 8/9):\n\n" + table +
        "\n\nHãy viết nhận xét ngắn (tối đa 250 từ) bằng tiếng Việt, văn phong khoa học, trung tính: "
        "(1) khu vực nóng/khô nhất, (2) thay đổi so với cùng kỳ năm trước, (3) hạn chế dữ liệu. "
        "Chỉ dựa trên số liệu trên, không suy diễn quan hệ nhân quả, không nêu số liệu không có trong bảng."
    )
    client = anthropic.Anthropic()          # đọc ANTHROPIC_API_KEY từ môi trường
    msg = client.messages.create(model="claude-sonnet-5-5", max_tokens=1000,
                                 messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in msg.content if b.type == "text")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"# Nhận xét tháng {latest:%m/%Y} (do AI tạo, cần kiểm tra lại)\n\n{text}\n\n```\n{table}\n```\n")
    return out_path
