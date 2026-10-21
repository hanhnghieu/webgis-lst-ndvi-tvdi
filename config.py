"""Cấu hình chung. Sửa COL_* cho khớp tên cột trong CSDL của bạn."""
import os
import re
from dotenv import load_dotenv

load_dotenv()


def ident(name: str) -> str:
    """Chỉ cho phép tên bảng/cột hợp lệ (tránh SQL injection từ .env)."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"Tên không hợp lệ: {name!r}")
    return name


# ---- GEE ----
GEE_PROJECT = os.getenv("GEE_PROJECT", "")
GAUL_ADM1_NAMES = ["Ho Chi Minh City", "Binh Duong", "Ba Ria-Vung Tau"]# kiểm tra lại tên trong GAUL
BBOX = [106.0, 10.1, 107.7, 11.8]          # loại Côn Đảo
EE_CRS = "EPSG:32648"
MAX_CLOUD = 70                              # CLOUD_COVER < 70 %
GRID_SCALE_M = 1500                         # khoảng cách lưới điểm (m); nhỏ hơn -> nhiều điểm hơn
MIN_VALID_RATIO = 0.30                      # bỏ tháng có valid_ratio thấp hơn
START_YEAR = 2016
FINAL_AFTER_DAY = 10                         # tháng trước được "chốt" sau ngày này của tháng sau (chờ ảnh về muộn)

# ---- TVDI ----
NDVI_BIN = 0.05
NDVI_EDGE_MAX = 0.90
MIN_PTS_PER_BIN = 5
MIN_BINS = 6

# ---- Bảng (theo Bảng 6.1 của báo cáo) ----
TBL_DIEM = ident(os.getenv("TBL_DIEM", "luoi_diem_quan_trac"))
TBL_ANH = ident(os.getenv("TBL_ANH", "thong_tin_anh_ve_tinh"))
TBL_VUNG = ident(os.getenv("TBL_VUNG", "ranh_gioi_khong_gian"))
TBL_DUBAO = ident(os.getenv("TBL_DUBAO", "du_lieu_du_bao"))
TBL_TRANGTHAI = ident(os.getenv("TBL_TRANGTHAI", "trang_thai_cap_nhat"))   # bảng phụ, tự tạo
GEOM_SRID = int(os.getenv("GEOM_SRID", "4326"))

# ---- Cột ----
COL_DIEM = dict(scene="ma_canh_anh", vung="ma_khu_vuc", ngay="ngay_quan_trac",
                lst="lst", ndvi="ndvi", tvdi="tvdi", geom="geom")
COL_ANH = dict(id="ma_canh_anh", ngay="ngay_chup", may="do_phu_may", sun="goc_mat_troi")
COL_VUNG = dict(id="ma_khu_vuc", geom="geom")
COL_DUBAO = dict(vung="ma_khu_vuc", chi_so="loai_chi_so", ngay="ngay_du_bao",
                 gia_tri="gia_tri_du_bao", mo_hinh="mo_hinh", duoi="can_duoi",
                 tren="can_tren", kich_ban="kich_ban", ref="ngay_tham_chieu")
for _d in (COL_DIEM, COL_ANH, COL_VUNG, COL_DUBAO):
    for _k, _v in _d.items():
        ident(_v)

OUT_DIR = os.getenv("OUT_DIR", "output")
