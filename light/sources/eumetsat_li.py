"""
light.sources.eumetsat_li — EUMETSAT MTG-I1 闪电成像仪（LI）Level 2 闪击产品。

实测（2026-09-17）：
  * 检索 OpenSearch **匿名可读**：
      GET https://api.eumetsat.int/data/search-products/1.0.0/os
          ?pi=EO:EUM:DAT:0691&format=json&si=0&c=N
  * 元数据匿名可读（/metadata、/metadata?format=json）
  * **产品本体下载需要 OAuth2 Bearer token**
  * 每个产品覆盖 **10 分钟**，513 KB（LFL），窗口结束后约 40 秒上架
  * 产品是 ZIP，内含 *BODY*.nc（NetCDF）+ trailer + quicklook + manifest

认证（需要 EUMETSAT_CONSUMER_KEY / EUMETSAT_CONSUMER_SECRET）：
    POST https://api.eumetsat.int/token
    Authorization: Basic base64(key:secret)
    body: grant_type=client_credentials
  -> {"access_token": "...", "expires_in": 3600}

变量名做了防御式自动探测：不同产品（LFL/LGR/LEF）的经纬度变量名可能不同，
所以按正则匹配而不是硬编码；首次实跑时把探测结果打进日志。
"""
from __future__ import annotations

import base64
import concurrent.futures
import io
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone

from light import config

logger = logging.getLogger(__name__)

_TOKEN_CACHE: dict[str, object] = {"token": None, "expires": 0.0}

_LAT_RE = re.compile(r"^(flash_|group_|event_)?latitude$|^(flash_|group_|event_)?lat$", re.I)
_LON_RE = re.compile(r"^(flash_|group_|event_)?longitude$|^(flash_|group_|event_)?lon$", re.I)
_QUAL_RE = re.compile(r"quality|confidence", re.I)


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------

def get_token(force: bool = False) -> str | None:
    """取（并缓存）OAuth2 access token。失败返回 None。"""
    import time
    if not config.eumetsat_enabled():
        logger.info("EUMETSAT: 未配置 EUMETSAT_CONSUMER_KEY/SECRET，跳过")
        return None

    now = time.time()
    cached = _TOKEN_CACHE.get("token")
    if cached and not force and now < float(_TOKEN_CACHE.get("expires", 0)) - 60:
        return str(cached)

    basic = base64.b64encode(
        f"{config.EUMETSAT_KEY}:{config.EUMETSAT_SECRET}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(
        config.EUMETSAT_TOKEN_URL, data=body,
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200]
        logger.error("EUMETSAT token 失败 HTTP %s: %s", exc.code, detail)
        return None
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        logger.error("EUMETSAT token 失败: %s: %s", type(exc).__name__, exc)
        return None

    tok = data.get("access_token")
    if not tok:
        logger.error("EUMETSAT token 响应缺少 access_token: %s", str(data)[:200])
        return None
    _TOKEN_CACHE["token"] = tok
    _TOKEN_CACHE["expires"] = now + float(data.get("expires_in", 3600))
    logger.info("EUMETSAT: 已获取 token（%s 秒有效）", data.get("expires_in"))
    return str(tok)


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------

def search(dt_start: datetime, dt_end: datetime, token: str | None) -> list[dict]:
    """检索窗口内的产品条目（匿名即可）。"""
    q = {
        "pi": config.EUMETSAT_COLLECTION,
        "format": "json",
        "si": "0",
        "c": str(max(config.EUMETSAT_MAX_PRODUCTS * 4, 24)),
        "dtstart": dt_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dtend": dt_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    url = config.EUMETSAT_SEARCH_URL + "?" + urllib.parse.urlencode(q)
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = json.loads(resp.read())
    except (urllib.error.HTTPError, urllib.error.URLError, OSError,
            json.JSONDecodeError) as exc:
        logger.warning("EUMETSAT 检索失败: %s: %s", type(exc).__name__, exc)
        return []
    feats = data.get("features") or []
    logger.info("EUMETSAT: 命中 %s 条，取 %d 条",
                data.get("totalResults"), len(feats))
    return feats


def _entry_window(entry: dict) -> tuple[datetime, datetime] | None:
    d = (entry.get("properties") or {}).get("date") or ""
    if "/" not in d:
        return None
    a, b = d.split("/", 1)
    try:
        s = datetime.strptime(a[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        e = datetime.strptime(b[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return s, e
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 下载 + 解析
# ---------------------------------------------------------------------------

def _parse_body(blob: bytes, tag: str) -> list[tuple[float, float, str, float]]:
    """从 ZIP 里取出 BODY NetCDF 并抽取经纬度。"""
    import h5py
    import numpy as np

    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        logger.warning("EUMETSAT: 不是有效 ZIP: %s", exc)
        return []

    body = None
    for name in zf.namelist():
        up = name.upper()
        if up.endswith(".NC") and "BODY" in up:
            body = name
            break
    if body is None:
        for name in zf.namelist():
            if name.upper().endswith(".NC"):
                body = name
                break
    if body is None:
        logger.warning("EUMETSAT: ZIP 里没有 NetCDF，成员=%s", zf.namelist()[:6])
        return []

    try:
        with h5py.File(io.BytesIO(zf.read(body)), "r") as h:
            names = list(h.keys())
            lat_name = next((n for n in names if _LAT_RE.match(n)), None)
            lon_name = next((n for n in names if _LON_RE.match(n)), None)
            qual_name = next((n for n in names if _QUAL_RE.search(n)), None)
            if not lat_name or not lon_name:
                logger.warning("EUMETSAT: 找不到经纬度变量。成员=%s", names[:40])
                return []
            lat = np.asarray(h[lat_name][:], dtype="float64").ravel()
            lon = np.asarray(h[lon_name][:], dtype="float64").ravel()
            qual = None
            if qual_name:
                try:
                    qual = np.asarray(h[qual_name][:]).ravel()
                except Exception:                 # noqa: BLE001
                    qual = None
    except Exception as exc:                      # noqa: BLE001
        logger.warning("EUMETSAT: NetCDF 解析失败: %s: %s", type(exc).__name__, exc)
        return []

    if lat.size == 0:
        logger.info("EUMETSAT: %s -> 0 个闪击（变量 %s/%s）",
                    tag, lat_name, lon_name)
        return []

    n = min(lat.size, lon.size)
    lat, lon = lat[:n], lon[:n]
    mask = np.isfinite(lat) & np.isfinite(lon) & (lat >= -90) & (lat <= 90)
    if qual is not None and qual.size >= n:
        # LI 的 quality/confidence 语义与 GLM 不同，这里只剔除明显无效值
        mask &= np.isfinite(qual)
    lat, lon = lat[mask], lon[mask]
    logger.info("EUMETSAT: %s -> %d 个闪击（lat=%s lon=%s qual=%s）",
                tag, lat.size, lat_name, lon_name, qual_name)
    return [(float(a), float(b), "mtg-li", 1.0) for a, b in zip(lat, lon)]


def _download_one(entry: dict, token: str) -> list[tuple[float, float, str, float]]:
    links = ((entry.get("links") or {}).get("data") or [])
    href = links[0].get("href") if links else None
    if not href:
        return []
    eid = entry.get("id", "?")
    req = urllib.request.Request(href, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=config.EUMETSAT_TIMEOUT) as resp:
            blob = resp.read()
    except urllib.error.HTTPError as exc:
        logger.warning("EUMETSAT 下载失败 HTTP %s (%s)", exc.code, str(eid)[:50])
        return []
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("EUMETSAT 下载失败 %s: %s", type(exc).__name__, exc)
        return []
    return _parse_body(blob, str(eid)[-40:])


def fetch(dt_end: datetime, window_minutes: int
          ) -> list[tuple[float, float, str, float]]:
    """取窗口内的 MTG LI 闪击。未配置密钥或任何失败都返回空列表。"""
    from datetime import timedelta

    if dt_end.tzinfo is None:
        dt_end = dt_end.replace(tzinfo=timezone.utc)
    t_end = dt_end
    t_start = t_end - timedelta(minutes=window_minutes)

    token = get_token()
    if not token:
        return []

    entries = search(t_start, t_end, token)
    if not entries:
        return []

    wanted = []
    for e in entries:
        w = _entry_window(e)
        if w and w[0] < t_end and w[1] > t_start:
            wanted.append(e)
    if len(wanted) > config.EUMETSAT_MAX_PRODUCTS:
        wanted = wanted[:config.EUMETSAT_MAX_PRODUCTS]
    if not wanted:
        logger.warning("EUMETSAT: 检索到 %d 条但没有落在窗口内的", len(entries))
        return []

    logger.info("EUMETSAT: 下载 %d 个产品（%s .. %s），并发 %d",
                len(wanted), t_start.strftime("%m-%d %H:%M"),
                t_end.strftime("%m-%d %H:%M"), config.EUMETSAT_CONCURRENCY)

    pts: list[tuple[float, float, str, float]] = []
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=config.EUMETSAT_CONCURRENCY) as pool:
        for res in pool.map(lambda e: _download_one(e, token), wanted):
            pts.extend(res)

    logger.info("EUMETSAT: %d 个产品 -> %d 个闪击点", len(wanted), len(pts))
    return pts
