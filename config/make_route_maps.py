# -*- coding: utf-8 -*-
"""
生成 ATC综合数据中心 可识别的雷达地图(GR格式 .txt):
  - DepRoute.txt  : ZGSZ 出港国内航线图(GR3 航路 + 点名称)
  - ArrRoute.txt  : ZGSZ 进港国内航线图(GR3 航路 + 点名称)
  - WorldMap.txt  : 世界海岸线雷达地图(GP29 多边形)
数据源:
  - config/城市对航线.txt : 起飞地<TAB>目的地<TAB>航线(航路点空格分隔)
  - config/航路点坐标.txt : 航路点 经度 纬度 (度,分,秒)
  - config/机场.xls       : 机场坐标 (DDDMMSS 拼接格式, 无符号, 符号按 airportsdata/国家推断)
规则:
  - 同城市对多条航线只取第一条; 仅 ZGSZ 作为起飞地(DepRoute) / 目的地(ArrRoute)
  - 仅国内航线(对端机场四字码以 Z 开头); 国外机场忽略
  - 航线航路点少于 4 个直接跳过
  - 机场坐标缺失(含 0,0)的航线忽略
  - 航线上每个点标注名称(端点标机场四字码, 存 GRR 名称字段)
格式(与 map/ROUTES.txt、map/China.txt 一致, 解析器见 index.html parseMapFile):
  - GR3 <名称> <线型> <线宽> <点数> <航路宽度>  + GRR <纬度> <经度> <点名称>
  - GP29 <名称> <线型> <填充> <点数> <线宽>     + GPP <纬度> <经度>
  - 坐标度分秒: 22,38,21.42N / 113,48,38.32E
  - 色号: COLORS[31] 调色板 (GR3=灰同 ROUTES.txt, GP29=深蓝同 PNG 国界)
运行: ~/.pyenv/versions/3.10.14/bin/python3 make_route_maps.py
"""
import math, os, re, zlib, json, sqlite3
from collections import Counter

BASE = os.path.dirname(os.path.abspath(__file__))
ROUTES_F = os.path.join(BASE, "城市对航线.txt")
COORDS_F = os.path.join(BASE, "航路点坐标.txt")
AIRPORT_XLS = os.path.join(BASE, "机场.xls")

C_ROUTE = 3      # GR3 灰, 同系统 ROUTES.txt
C_COUNTRY = 29   # GP29 国界 (39,79,114)
LINE_STYLE = 0   # 实线
LINE_WIDTH = 1

# ══════════════ 1. 航路点坐标 (度分秒) ══════════════
_PAT = re.compile(r"^(\d+),(\d+),([\d.]+)([EW])\s+(\d+),(\d+),([\d.]+)([NS])")
wp_coords = {}
with open(COORDS_F, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        m = _PAT.match(" ".join(parts[1:3])) if len(parts) >= 3 else None
        if not m:
            continue
        d, mi, s, h = int(m.group(1)), int(m.group(2)), float(m.group(3)), m.group(4)
        lon = (d + mi/60 + s/3600) * (-1 if h == "W" else 1)
        d, mi, s, h = int(m.group(5)), int(m.group(6)), float(m.group(7)), m.group(8)
        lat = (d + mi/60 + s/3600) * (-1 if h == "S" else 1)
        wp_coords.setdefault(parts[0], (lat, lon))
print("航路点:", len(wp_coords))

# ══════════════ 2. 机场坐标 (机场.xls + 符号推断) ══════════════
import xlrd, airportsdata
ad = airportsdata.load("ICAO")

def dms_to_deg(v, nd):
    s = str(v).strip()
    d = int(s[:nd]); m = int(s[nd:nd+2]); sec = float(s[nd+2:])
    return d + m/60 + sec/3600

wb = xlrd.open_workbook(AIRPORT_XLS)
sh = wb.sheet_by_index(0)
XLS = {}
for r in range(1, sh.nrows):
    code = str(sh.cell_value(r, 0)).strip()
    XLS[code] = (str(sh.cell_value(r, 3)).strip(),
                 str(sh.cell_value(r, 10)).strip(),
                 str(sh.cell_value(r, 11)).strip())

_air_sign = {}
for code, (country, lon_s, lat_s) in XLS.items():
    if lon_s in ("", "None") or lat_s in ("", "None"):
        continue
    try:
        lon_mag = dms_to_deg(lon_s, 3); lat_mag = dms_to_deg(lat_s, 2)
    except Exception:
        continue
    if lon_mag < 0.0005 and lat_mag < 0.0005:
        continue
    a = ad.get(code)
    if a:
        _air_sign[code] = (1 if a["lon"] >= 0 else -1, 1 if a["lat"] >= 0 else -1)

_cn_lon, _cn_lat = Counter(), Counter()
for code, s in _air_sign.items():
    country = XLS[code][0]
    _cn_lon[(country, s[0])] += 1
    _cn_lat[(country, s[1])] += 1
country_sign = {}
for country in {c for c, _, _ in XLS.values()}:
    e = _cn_lon.get((country, 1), 0); w = _cn_lon.get((country, -1), 0)
    n = _cn_lat.get((country, 1), 0); s = _cn_lat.get((country, -1), 0)
    if e + w + n + s == 0:
        continue
    country_sign[country] = (1 if e >= w else -1, 1 if n >= s else -1)
country_sign.update({
    "科威特": (1, 1), "乌兹别克斯坦": (1, 1), "马拉维": (1, -1), "法属索马里": (1, 1),
})
print("机场.xls:", len(XLS), " 国家定号表:", len(country_sign))

def airport_xy(code):
    row = XLS.get(code)
    if not row:
        return None
    country, lon_s, lat_s = row
    if lon_s in ("", "None") or lat_s in ("", "None"):
        return None
    try:
        lon_mag = dms_to_deg(lon_s, 3); lat_mag = dms_to_deg(lat_s, 2)
    except Exception:
        return None
    if lon_mag < 0.0005 and lat_mag < 0.0005:
        return None
    s = _air_sign.get(code)
    if s is None:
        s = country_sign.get(country)
    if s is None:
        return None
    return (lat_mag * s[1], lon_mag * s[0])

print("ZGSZ 坐标:", airport_xy("ZGSZ"))

# ══════════════ 3. 航线解析 + 去重 + 过滤(国内 + 航路点>=4) ══════════════
def parse_routes():
    out = []
    with open(ROUTES_F, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            p = line.split("\t")
            if len(p) < 3:
                continue
            out.append((p[0], p[1], p[2].split()))
    return out

raw = parse_routes()

def extract(kind):
    """kind='dep': ZGSZ 起飞; kind='arr': ZGSZ 到达
    城市对取首条; 对端必须 Z 开头(国内); 航路点 >= 4"""
    seen, out = set(), []
    for dep, dest, wps in raw:
        if kind == "dep" and dep != "ZGSZ":
            continue
        if kind == "arr" and dest != "ZGSZ":
            continue
        other = dest if kind == "dep" else dep
        if not other.startswith("Z"):
            continue                      # 国外忽略
        if len(wps) < 4:
            continue                      # 航路点少于4跳过
        key = (dep, dest)
        if key in seen:
            continue
        seen.add(key)
        out.append((dep, dest, wps))
    return out

dep_routes = extract("dep")
arr_routes = extract("arr")
print(f"出港(国内,≥4航路点,去重): {len(dep_routes)}  进港: {len(arr_routes)}")

def build(routes):
    """返回 (polylines带点名, skipped)"""
    polys, skipped = [], []
    for dep, dest, wps in routes:
        d0 = airport_xy(dep)
        d1 = airport_xy(dest)
        if d0 is None or d1 is None:
            skipped.append((dep, dest, "机场坐标缺失"))
            continue
        pts = [(d0[0], d0[1], dep)]
        for wp in wps:
            if wp in wp_coords:
                la, lo = wp_coords[wp]
                pts.append((la, lo, wp))
        pts.append((d1[0], d1[1], dest))
        if len(pts) < 2:
            skipped.append((dep, dest, "航路点缺失"))
            continue
        polys.append((dep, dest, pts))
    return polys, skipped

dep_polys, dep_skip = build(dep_routes)
arr_polys, arr_skip = build(arr_routes)
print("出港可绘:", len(dep_polys), " 忽略:", len(dep_skip))
print("进港可绘:", len(arr_polys), " 忽略:", len(arr_skip))
for label, skips in (("出港", dep_skip), ("进港", arr_skip)):
    codes = sorted({(d if d != "ZGSZ" else t) for d, t, _ in skips})
    print(f"  {label}忽略机场({len(codes)}):", " ".join(codes))

# ══════════════ 4. 跨日界线拆分(国内航线不会触发, 保留兼容) ══════════════
def split_dateline(pts):
    if len(pts) < 2:
        return [pts]
    chains = []
    cur = [pts[0]]
    for lat, lon, nm in pts[1:]:
        plat, plon = cur[-1][0], cur[-1][1]
        d = (lon - plon + 180) % 360 - 180
        target = plon + d
        if target > 180 or target < -180:
            if d > 0:
                frac = (180 - plon) / d
                elat = plat + frac * (lat - plat)
                cur.append((elat, 180, ""))
                chains.append(cur)
                cur = [(elat, -180, ""), (lat, target - 360, nm)]
            else:
                frac = (-180 - plon) / d
                elat = plat + frac * (lat - plat)
                cur.append((elat, -180, ""))
                chains.append(cur)
                cur = [(elat, 180, ""), (lat, target + 360, nm)]
        else:
            cur.append((lat, target, nm))
    chains.append(cur)
    return chains

# ══════════════ 5. 世界海岸线数据 ══════════════
import pyworldatlas_mapdata_standard as _pw
DB = os.path.join(os.path.dirname(_pw.__file__), "data", "maps.sqlite3")
con = sqlite3.connect(DB)
world_rings = []
for alpha2, name, payload in con.execute("SELECT alpha2,name,payload FROM country_map"):
    try:
        data = json.loads(zlib.decompress(payload))
        for ring in data["boundary"]:
            if len(ring) >= 3:
                world_rings.append((name, ring))
    except Exception:
        pass
print("世界海岸线环:", len(world_rings))

# ══════════════ 6. 度分秒输出 ══════════════
def to_dms(deg, hemi_pos, hemi_neg):
    h = hemi_pos if deg >= 0 else hemi_neg
    v = abs(deg)
    d = int(v); m = int((v - d) * 60)
    s = (v - d - m / 60) * 3600
    s = round(s, 2)
    if s >= 60:
        s -= 60; m += 1
    if m >= 60:
        m -= 60; d += 1
    return f"{d},{m:02d},{s:05.2f}{h}"

# ══════════════ 7. 航线雷达地图 (GR3 + GRR 带点名称 + GST 航路点标注) ══════════════
def write_route_map(polys, out):
    waypoints = {}   # name -> (lat, lon)  去重
    with open(out, "w", encoding="utf-8") as f:
        f.write("//ATC综合数据中心雷达地图  GR3=航路(灰) 0线型 1线宽 点数 0航路宽度\n")
        f.write("//GR3  名称  线型  线宽  端点个数  航路宽度(公里)  GRR 纬度 经度 点名称\n")
        for i, (dep, dest, pts) in enumerate(polys, 1):
            chains = split_dateline(pts)
            name = f"{dep}-{dest}"
            for ci, ch in enumerate(chains):
                nm = name if len(chains) == 1 else f"{name}-{ci+1}"
                f.write(f"GR3\t{nm}\t{LINE_STYLE}\t{LINE_WIDTH}\t{len(ch)}\t0\n")
                for lat, lon, pname in ch:
                    f.write(f"GRR\t{to_dms(lat, 'N', 'S')}\t{to_dms(lon, 'E', 'W')}\t{pname}\n")
                    if pname in wp_coords:      # 航路点(非机场端点)收集去重
                        waypoints.setdefault(pname, (lat, lon))
        # 尾部追加 GST 航路点标注(去重, 按名称排序)
        f.write("\n//航路点标注(去重)  GST19=黑 名称 名称 3(小圆点) 1\n")
        for pname in sorted(waypoints):
            lat, lon = waypoints[pname]
            f.write(f"GST19\t{to_dms(lat, 'N', 'S')}\t{to_dms(lon, 'E', 'W')}\t{pname}\t{pname}\t3\t1\n")
    print(f"已保存: {out} (航线{len(polys)}条, 航路点标注{len(waypoints)}个)")

write_route_map(dep_polys, os.path.join(BASE, "DepRoute.txt"))
write_route_map(arr_polys, os.path.join(BASE, "ArrRoute.txt"))

# ══════════════ 8. WorldMap.txt 世界海岸线雷达地图 ══════════════
def write_world_map(out):
    with open(out, "w", encoding="utf-8") as f:
        f.write("//世界海岸线雷达地图(Natural Earth 1:10m, 5分角重采样)  GP29=国界 0线型 0填充 点数 1线宽\n")
        f.write("//GP29  名称  线型  填充  点数  线宽  GPP 纬度 经度\n")
        n_blocks = n_pts = 0
        for name, ring in world_rings:
            pts = [(lat, lon) for lon, lat in ring]
            f.write(f"GP29\t{name}\t0\t0\t{len(pts)}\t1\n")
            for lat, lon in pts:
                f.write(f"GPP\t{to_dms(lat, 'N', 'S')}\t{to_dms(lon, 'E', 'W')}\t1\n")
            n_blocks += 1
            n_pts += len(pts)
    print(f"已保存: {out} (GP块={n_blocks}, 点={n_pts})")

write_world_map(os.path.join(BASE, "WorldMap.txt"))
print("完成")
