"""A-stock (China mainland) data vendor for TradingAgents.

Zero third-party data dependency (no akshare). All sources are direct HTTP APIs
or mootdx TCP.

Data sources:
- mootdx (TCP 7709): OHLCV K-lines, financial snapshots, F10 text
- Tencent Finance (HTTP GBK): PE/PB/market cap/turnover
- 东方财富 push2 / datacenter-web (direct HTTP): stock info, dragon-tiger, lockup
- 新浪财经 (direct HTTP): K-line fallback, financial statements
- 同花顺 (direct HTTP): consensus EPS, hot stocks, northbound capital flow
- 财联社 (direct HTTP): global news wire
"""

from __future__ import annotations

from typing import Annotated
from datetime import datetime
from dateutil.relativedelta import relativedelta
import json as _json
import os
import logging
import math
import re as _re
import time as _time
import uuid
import urllib.request

import pandas as pd
import requests as _requests_module

# 创建一个不读取环境变量代理的 Session，彻底避免 HTTP_PROXY 干扰国内数据源
_requests = _requests_module.Session()
_requests.trust_env = False

from .utils import safe_ticker_component

logger = logging.getLogger(__name__)

# 代理绕行：东财/新浪/百度等国内数据源不需要走代理，且代理常导致超时或 503
# 双重保障：1) 清除环境变量中的代理设置；2) 每个 request 调用显式传 proxies=_NO_PROXY
for _proxy_var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(_proxy_var, None)

_NO_PROXY = {"http": None, "https": None}


# ---------------------------------------------------------------------------
# Major market indices (指数板块)
# ---------------------------------------------------------------------------

# 预置主要指数：东财 secid 格式为 {market}.{code}
# market: 1=沪市, 0=深市
# type: "index" 用于区分指数和个股
INDEX_SECTORS = [
    {"code": "000001", "name": "上证指数",   "market": 1, "type": "index"},
    {"code": "399001", "name": "深证成指",   "market": 0, "type": "index"},
    {"code": "399006", "name": "创业板指",   "market": 0, "type": "index"},
    {"code": "000688", "name": "科创50",     "market": 1, "type": "index"},
    {"code": "899050", "name": "北证50",     "market": 0, "type": "index"},
    # 更多主要指数
    {"code": "000300", "name": "沪深300",    "market": 1, "type": "index"},
    {"code": "000905", "name": "中证500",    "market": 1, "type": "index"},
    {"code": "000852", "name": "中证1000",   "market": 1, "type": "index"},
    {"code": "000016", "name": "上证50",     "market": 1, "type": "index"},
    {"code": "399005", "name": "中小100",    "market": 0, "type": "index"},
    {"code": "399673", "name": "创业板50",   "market": 0, "type": "index"},
]

# 快速查找: code -> index info
_INDEX_MAP: dict[str, dict] = {s["code"]: s for s in INDEX_SECTORS}

# K线请求间隔(秒)，避免连续请求触发限流
_KLINE_REQUEST_INTERVAL = 0.3


# ---------------------------------------------------------------------------
# Helpers: ticker format & market detection
# ---------------------------------------------------------------------------

def _get_prefix(code: str) -> str:
    """6-digit A-stock code -> market prefix (sh/sz/bj) for API calls.
    
    Supports regular stocks and index codes.
    """
    # 已知指数优先查表
    idx = _INDEX_MAP.get(code)
    if idx:
        return "sh" if idx["market"] == 1 else "sz"
    # 北交所
    if code.startswith("8") or code.startswith("4"):
        return "bj"
    # 沪市: 6xx 主板, 688/689 科创板, 9xx (B股)
    if code.startswith(("6", "9")):
        return "sh"
    return "sz"


def _eastmoney_secid(code: str) -> str:
    """6-digit code -> eastmoney secid format '{market}.{code}'.
    
    Rules:
    - Regular stocks: 6xx/688/689 -> 1.{code} (沪), 0xx/3xx -> 0.{code} (深)
    - Indices: use _INDEX_MAP for market lookup
    - 8xx/4xx (北交所): 0.{code} (东财对北交所也用0)
    """
    idx = _INDEX_MAP.get(code)
    if idx:
        return f"{idx['market']}.{code}"
    # 北交所
    if code.startswith("8") or code.startswith("4"):
        return f"0.{code}"
    # 沪市
    if code.startswith("6") or code.startswith("9"):
        return f"1.{code}"
    # 深市
    return f"0.{code}"


def _is_index_code(code: str) -> bool:
    """判断代码是否为指数（非个股）。"""
    return code in _INDEX_MAP


def _normalize_ticker(symbol: str) -> str:
    """Strip exchange prefix/suffix, return pure 6-digit code.

    Handles: '688017', 'SH688017', '688017.SH', 'sh688017'
    """
    s = symbol.strip().upper()
    # Remove .SH / .SZ / .BJ suffix
    for suffix in (".SH", ".SZ", ".BJ"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    # Remove SH / SZ / BJ prefix
    for prefix in ("SH", "SZ", "BJ"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
    return safe_ticker_component(s)


# ---------------------------------------------------------------------------
# Stock name <-> code mapping (cached)
# ---------------------------------------------------------------------------

_name_to_code: dict[str, str] | None = None
_code_to_name: dict[str, str] | None = None


def _build_name_code_map() -> tuple[dict[str, str], dict[str, str]]:
    """Build name→code and code→name maps via mootdx (both SH & SZ markets).

    Falls back to an empty map if mootdx is unreachable. The resolve_ticker
    function will still work via EastMoney suggest API through search_stocks.
    """
    global _name_to_code, _code_to_name
    if _name_to_code is not None:
        return _name_to_code, _code_to_name

    n2c: dict[str, str] = {}
    c2n: dict[str, str] = {}

    # Primary: mootdx (full market coverage)
    try:
        from mootdx.quotes import Quotes

        client = Quotes.factory(market="std")
        for market in (0, 1):  # 0=SZ, 1=SH
            try:
                stocks = client.stocks(market=market)
                if stocks is None or stocks.empty:
                    continue
                for _, row in stocks.iterrows():
                    code = str(row["code"]).strip()
                    name = str(row["name"]).strip()
                    if not _re.match(r"^[036]\d{5}$", code):
                        continue
                    clean_name = name.replace(" ", "").replace("　", "")
                    n2c[clean_name] = code
                    c2n[code] = clean_name
            except Exception as e:
                logger.warning("mootdx market=%d stocks failed: %s", market, e)
    except Exception as e:
        logger.warning("mootdx unavailable, name-code map will be empty: %s", e)

    _name_to_code = n2c
    _code_to_name = c2n
    logger.info("Built stock name-code map: %d entries", len(n2c))
    return _name_to_code, _code_to_name


def resolve_ticker(user_input: str) -> str:
    """Resolve user input (code or Chinese name) to a 6-digit A-stock code.

    Accepts: '600379', 'SH600379', '600379.SH', '宝光股份', 'gzmt'
    Returns: '600379'
    Raises: ValueError if not resolvable.
    """
    s = user_input.strip()
    if not s:
        raise ValueError("输入不能为空")

    has_chinese = any("\u4e00" <= ch <= "\u9fff" for ch in s)

    # If purely alphanumeric (no Chinese), try as ticker code first
    if not has_chinese:
        try:
            code = _normalize_ticker(s)
            if _re.match(r"^[036]\d{5}$", code):
                return code
        except ValueError:
            pass
        # Not a valid 6-digit A-stock code — try search (pinyin, partial code, etc.)

    clean = s.replace(" ", "").replace("\u3000", "")
    n2c, _ = _build_name_code_map()

    if clean in n2c:
        return n2c[clean]

    matches = {name: code for name, code in n2c.items() if clean in name}
    if len(matches) == 1:
        return next(iter(matches.values()))
    if len(matches) > 1:
        examples = ", ".join(f"{n}({c})" for n, c in list(matches.items())[:5])
        raise ValueError(f"'{s}' 匹配到多只股票: {examples}，请输入完整名称或代码")

    # Fallback: use EastMoney API if local map is empty / no match
    results = search_stocks(clean, limit=1)
    if results:
        return results[0]["code"]

    raise ValueError(f"找不到股票 '{s}'，请检查名称是否正确")


# ---------------------------------------------------------------------------
# Stock search (code / Chinese name / pinyin)
# ---------------------------------------------------------------------------

def _eastmoney_suggest(query: str, count: int = 10) -> list[dict]:
    """Search stocks via East Money suggest API (realtime, no local index needed).

    Supports Chinese name, pinyin (full + initials), and code search.
    Returns list of {"code": str, "name": str} filtered to A-share 6-digit codes.
    """
    url = "https://searchapi.eastmoney.com/api/suggest/get"
    params = {
        "input": query,
        "type": "14",
        "token": "D43BF722C8E33BDC906FB84D85E326E8",
        "count": count * 2,  # over-fetch, filter later
    }
    try:
        r = _requests.get(url, params=params, timeout=5)
        data = r.json()
        items = data.get("QuotationCodeTable", {}).get("Data") or []
        results = []
        seen = set()
        for item in items:
            code = str(item.get("Code", "")).strip()
            name = str(item.get("Name", "")).strip()
            # Only keep A-share 6-digit codes (0xxxxx, 3xxxxx, 6xxxxx)
            if not _re.match(r"^[036]\d{5}$", code):
                continue
            if code in seen:
                continue
            seen.add(code)
            results.append({"code": code, "name": name})
            if len(results) >= count:
                break
        return results
    except Exception as e:
        logger.debug("EastMoney suggest API failed: %s", e)
        return []


# Pinyin search index — used as fallback when EastMoney API is unavailable
_pinyin_index: list[tuple[str, str, str, str]] | None = None
# Each entry: (code, name, pinyin_full, pinyin_initials)


def _build_pinyin_index() -> list[tuple[str, str, str, str]]:
    """Build pinyin search index from the name-code map.

    Returns a list of (code, name, pinyin_full, pinyin_initials) tuples.
    Built once and cached globally.  Only used as fallback.
    """
    global _pinyin_index
    if _pinyin_index is not None:
        return _pinyin_index

    from pypinyin import pinyin, Style

    n2c, _ = _build_name_code_map()
    if not n2c:
        _pinyin_index = []
        return _pinyin_index

    index: list[tuple[str, str, str, str]] = []
    for name, code in n2c.items():
        py_full = "".join(p[0] for p in pinyin(name, style=Style.NORMAL))
        py_init = "".join(p[0] for p in pinyin(name, style=Style.FIRST_LETTER))
        index.append((code, name, py_full, py_init))

    _pinyin_index = index
    logger.info("Built pinyin search index: %d entries", len(index))
    return _pinyin_index


def search_stocks(query: str, limit: int = 10) -> list[dict]:
    """Search A-share stocks by code, Chinese name, or pinyin.

    Supports:
    - 6-digit code (full or prefix): "600519", "600"
    - Chinese name (full or substring): "贵州茅台", "茅台"
    - Pinyin full (prefix): "guizhou" → 贵州茅台
    - Pinyin initials (prefix): "gzmt" → 贵州茅台, "gzm" → 贵州茅台

    Primary: EastMoney suggest API (realtime, no local index).
    Fallback: mootdx local name-code map + pypinyin index.

    Args:
        query: Search keyword (code / Chinese / pinyin).
        limit: Max number of results (default 10).

    Returns:
        List of {"code": str, "name": str} dicts, ranked by relevance.
    """
    q = query.strip()
    if not q:
        return []

    # --- Primary: EastMoney suggest API ---
    results = _eastmoney_suggest(q, count=limit)
    if results:
        return results

    # --- Fallback: local pinyin index ---
    logger.info("EastMoney suggest failed, falling back to local pinyin index")
    n2c, c2n = _build_name_code_map()
    if not n2c:
        return []

    index = _build_pinyin_index()
    if not index:
        return []

    q_lower = q.lower()

    # Score each candidate: higher = better match
    scored: list[tuple[int, str, str]] = []

    for code, name, py_full, py_init in index:
        score = 0

        if code == q:
            score = 1000
        elif code.startswith(q):
            score = 800
        elif name == q:
            score = 900
        elif q in name:
            score = 600
        elif py_full.startswith(q_lower):
            score = 400
        elif q_lower in py_full:
            score = 200
        elif py_init == q_lower:
            score = 500
        elif py_init.startswith(q_lower):
            score = 300
        elif q.isdigit() and len(q) >= 2 and q in code:
            score = 100

        if score > 0:
            scored.append((score, code, name))

    scored.sort(key=lambda x: (-x[0], x[1]))
    return [{"code": code, "name": name} for _, code, name in scored[:limit]]


# ---------------------------------------------------------------------------
# mootdx client (singleton)
# ---------------------------------------------------------------------------

_mootdx_client = None


def _get_mootdx_client():
    """Lazy-init mootdx Quotes client (TCP connection, reusable)."""
    global _mootdx_client
    if _mootdx_client is None:
        from mootdx.quotes import Quotes

        try:
            _mootdx_client = Quotes.factory(market="std")
        except Exception as e:
            logger.warning("mootdx client init failed (server unavailable): %s", e)
            _mootdx_client = False  # sentinel: don't retry
    return _mootdx_client if _mootdx_client is not False else None


# ---------------------------------------------------------------------------
# Tencent Finance API
# ---------------------------------------------------------------------------

def _tencent_quote(codes: list[str]) -> dict[str, dict]:
    """Batch real-time quotes from Tencent Finance (qt.gtimg.cn).

    Returns dict[code] -> {name, price, pe_ttm, pb, mcap_yi, ...}
    """
    prefixed = [f"{_get_prefix(c)}{c}" for c in codes]
    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    resp = urllib.request.urlopen(req, timeout=10)
    raw = resp.read().decode("gbk")

    result = {}
    for line in raw.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = key[2:]  # strip sh/sz/bj prefix
        result[code] = {
            "name": vals[1],
            "price": float(vals[3]) if vals[3] else 0,
            "last_close": float(vals[4]) if vals[4] else 0,
            "open": float(vals[5]) if vals[5] else 0,
            "change_pct": float(vals[32]) if vals[32] else 0,
            "high": float(vals[33]) if vals[33] else 0,
            "low": float(vals[34]) if vals[34] else 0,
            "turnover_pct": float(vals[38]) if vals[38] else 0,
            "pe_ttm": float(vals[39]) if vals[39] else 0,
            "mcap_yi": float(vals[44]) if vals[44] else 0,
            "float_mcap_yi": float(vals[45]) if vals[45] else 0,
            "pb": float(vals[46]) if vals[46] else 0,
            "limit_up": float(vals[47]) if vals[47] else 0,
            "limit_down": float(vals[48]) if vals[48] else 0,
            "pe_static": float(vals[52]) if vals[52] else 0,
        }
    return result


# ---------------------------------------------------------------------------
# Eastmoney Datacenter unified helper (龙虎榜/解禁 etc.)
# ---------------------------------------------------------------------------

_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _eastmoney_datacenter(
    report_name: str,
    columns: str = "ALL",
    filter_str: str = "",
    page_size: int = 50,
    sort_columns: str = "",
    sort_types: str = "-1",
) -> list[dict]:
    """东财数据中心统一查询 — 龙虎榜/解禁 共用."""
    params = {
        "reportName": report_name,
        "columns": columns,
        "filter": filter_str,
        "pageNumber": "1",
        "pageSize": str(page_size),
        "sortColumns": sort_columns,
        "sortTypes": sort_types,
        "source": "WEB",
        "client": "WEB",
    }
    r = _requests.get(
        _DATACENTER_URL, params=params, headers={"User-Agent": _UA}, timeout=15,
        proxies=_NO_PROXY
    )
    d = r.json()
    if d.get("result") and d["result"].get("data"):
        return d["result"]["data"]
    return []


# ---------------------------------------------------------------------------
# 同花顺 EPS forecast helper (direct HTTP, no akshare)
# ---------------------------------------------------------------------------


def _ths_eps_forecast(code: str) -> pd.DataFrame:
    """Fetch consensus EPS forecast from 同花顺 (direct HTTP).

    Returns DataFrame with columns roughly: 年度, 预测机构数, 最小值, 均值, 最大值.
    """
    url = f"https://basic.10jqka.com.cn/new/{code}/worth.html"
    headers = {
        "User-Agent": _UA,
        "Referer": "https://basic.10jqka.com.cn/",
    }
    try:
        r = _requests.get(url, headers=headers, timeout=15,
                          proxies=_NO_PROXY)
        r.encoding = "gbk"
        # 检测反爬: 如果返回的是完整 HTML 页面（而非数据表格），跳过
        if "<!DOCTYPE" in r.text or "<html" in r.text.lower():
            logger.warning("同花顺 EPS 页面返回 HTML (反爬), code=%s", code)
            return pd.DataFrame()
        dfs = pd.read_html(r.text)
        # Find the table containing EPS data
        for df in dfs:
            cols = [str(c) for c in df.columns]
            if any("每股收益" in c or "均值" in c for c in cols):
                return df
        # Fallback: return first table if exists
        return dfs[0] if dfs else pd.DataFrame()
    except Exception as e:
        logger.warning("同花顺 EPS forecast failed for %s: %s", code, e)
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Multi-source K-line fetching (东方财富 / 新浪 / 腾讯 / Tushare)
# ---------------------------------------------------------------------------

# Scale mapping: level → API parameter
_KLINE_SCALE = {
    "1min": "1", "5min": "5", "15min": "15", "30min": "30", "60min": "60",
    "daily": "240", "weekly": "1200", "monthly": "5200",
}

# Eastmoney klt mapping (分钟级编码)
_EASTMONEY_KLT = {
    "1min": 1, "5min": 5, "15min": 15, "30min": 30, "60min": 60,
    "daily": 101, "weekly": 102, "monthly": 103,
}

# Fallback order by level
_KLINE_FALLBACK_MINUTE = ["tickflow", "eastmoney", "sina", "tencent", "tushare"]
_KLINE_FALLBACK_DAILY = ["tickflow", "sina", "tencent", "tushare", "eastmoney"]


def _eastmoney_kline(code: str, level: str = "daily",
                     start_date: str = None, datalen: int = 10000) -> pd.DataFrame:
    """从东方财富获取K线数据（支持1分~日线）。

    东方财富 push2his K线接口，日线最多10000根，分钟级最优。
    自动识别指数/个股，使用正确的 secid 和复权参数。

    Returns:
        DataFrame with columns: Date, Open, High, Low, Close, Volume.
    """
    secid = _eastmoney_secid(code)
    klt = _EASTMONEY_KLT.get(level, 101)
    # 指数不需要复权 (fqt=0)，个股前复权 (fqt=1)
    fqt = "0" if _is_index_code(code) else "1"

    url = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid,
        "klt": str(klt),
        "fqt": fqt,
        "lmt": str(datalen),
        "end": "20500101",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
    }

    try:
        r = _requests.get(url, params=params, headers={"User-Agent": _UA}, timeout=30,
                          proxies=_NO_PROXY)
        r.raise_for_status()
    except Exception as e:
        # SSL errors common in some environments — try without verify
        try:
            r = _requests.get(url, params=params, headers={"User-Agent": _UA},
                              timeout=30, verify=False, proxies=_NO_PROXY)
            r.raise_for_status()
        except Exception:
            logger.warning("Eastmoney kline failed for %s/%s: %s", code, level, e)
            return pd.DataFrame()
    d = r.json()

    klines = d.get("data", {}).get("klines", [])
    if not klines:
        return pd.DataFrame()

    rows = []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 7:
            continue
        rows.append({
            "Date": parts[0],
            "Open": float(parts[1]),
            "Close": float(parts[2]),
            "High": float(parts[3]),
            "Low": float(parts[4]),
            "Volume": int(float(parts[5])),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["Date"] = pd.to_datetime(df["Date"])

    if start_date:
        df = df[df["Date"] >= pd.to_datetime(start_date)]

    return df


def _sina_kline(code: str, level: str = "daily",
                start_date: str = None, datalen: int = 2000) -> pd.DataFrame:
    """从新浪财经获取K线数据（1分~日线，速度快）。

    Returns:
        DataFrame with columns: Date, Open, High, Low, Close, Volume.
    """
    prefix = _get_prefix(code)
    scale = _KLINE_SCALE.get(level, "240")
    url = (
        "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData"
    )
    params = {
        "symbol": f"{prefix}{code}",
        "scale": scale,
        "ma": "no",
        "datalen": str(datalen),
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
        "Referer": "https://finance.sina.com.cn/",
    }
    r = _requests.get(url, params=params, headers=headers, timeout=30,
                      proxies=_NO_PROXY)
    if r.status_code in (403, 456):
        # 新浪反爬封禁（456=IP异常访问），直接返回空，不抛异常
        logger.debug("Sina kline blocked for %s (HTTP %s)", code, r.status_code)
        return pd.DataFrame()
    r.raise_for_status()
    try:
        data = _json.loads(r.text)
    except (ValueError, _json.JSONDecodeError):
        return pd.DataFrame()

    if not data:
        return pd.DataFrame()

    rows = []
    for item in data:
        rows.append({
            "Date": item["day"],
            "Open": float(item["open"]),
            "High": float(item["high"]),
            "Low": float(item["low"]),
            "Close": float(item["close"]),
            "Volume": int(item["volume"]),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["Date"] = pd.to_datetime(df["Date"])

    if start_date:
        df = df[df["Date"] >= pd.to_datetime(start_date)]

    return df


def _tencent_kline(code: str, level: str = "daily",
                   start_date: str = None, datalen: int = 2000) -> pd.DataFrame:
    """从腾讯财经获取K线数据（1分~周线，稳定）。

    Returns:
        DataFrame with columns: Date, Open, High, Low, Close, Volume.
    """
    prefix = _get_prefix(code)
    # 腾讯 K线 type: 1=1分, 5=5分, 15=15分, 30=30分, 60=60分, day=日线, week=周线
    _TENCENT_TYPE = {
        "1min": "1", "5min": "5", "15min": "15", "30min": "30", "60min": "60",
        "daily": "day", "weekly": "week",
    }
    ktype = _TENCENT_TYPE.get(level, "day")

    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    # 指数不需要复权
    # param 格式: {prefix}{code},{ktype},,{datalen},{qfq}
    # 注意: 2025 起腾讯 API 不再接受中间的 ",1" 参数，
    #   旧格式 sz300007,day,,200,1,qfq → param error
    #   新格式 sz300007,day,,,200,qfq → 正常返回
    # 注意: datalen 过大（>2000）也会报 param error，需截断
    capped_datalen = min(datalen, 2000)
    qfq = ",qfq" if not _is_index_code(code) else ""
    params = {
        "_var": f"kline_{ktype}",
        "param": f"{prefix}{code},{ktype},,,{capped_datalen}{qfq}",
    }

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
    }

    try:
        r = _requests.get(url, params=params, headers=headers, timeout=30,
                          proxies=_NO_PROXY)
        r.raise_for_status()
    except Exception as e:
        # SSL errors — retry without verify
        try:
            r = _requests.get(url, params=params, headers=headers, timeout=30,
                              verify=False, proxies=_NO_PROXY)
            r.raise_for_status()
        except Exception:
            logger.warning("Tencent kline failed for %s/%s: %s", code, level, e)
            return pd.DataFrame()

    # 腾讯返回 js 变量赋值格式: kline_day="..."
    text = r.text.strip()
    eq_pos = text.find("=")
    if eq_pos < 0:
        return pd.DataFrame()
    json_str = text[eq_pos + 1:].strip().rstrip(";")
    try:
        d = _json.loads(json_str)
    except (ValueError, _json.JSONDecodeError):
        return pd.DataFrame()

    # 腾讯 API 返回 {"code":0,"msg":"param error"} 时，说明参数不合法
    if d.get("msg") and d.get("code", -1) == 0 and not d.get("data"):
        raise ValueError(f"Tencent API param error (datalen={datalen})")

    # 提取数据: d["data"][prefix+code]["qfqday"] 或 ["qfqweek"] 或 ["day"] / ["week"] (指数)
    raw_data = d.get("data", {})
    if isinstance(raw_data, list):
        # Some responses return data as list — not usable
        return pd.DataFrame()

    stock_data = raw_data.get(prefix + code, {})
    # 前复权数据优先（个股），指数用原始数据
    if _is_index_code(code):
        klines = stock_data.get(ktype, stock_data.get(f"qfq{ktype}", []))
    else:
        klines = stock_data.get(f"qfq{ktype}", stock_data.get(ktype, []))

    if not klines:
        return pd.DataFrame()

    rows = []
    for item in klines:
        rows.append({
            "Date": item[0],
            "Open": float(item[1]),
            "Close": float(item[2]),
            "High": float(item[3]),
            "Low": float(item[4]),
            "Volume": int(float(item[5])) if len(item) > 5 else 0,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["Date"] = pd.to_datetime(df["Date"])

    if start_date:
        df = df[df["Date"] >= pd.to_datetime(start_date)]

    return df


def _tushare_kline(code: str, level: str = "daily",
                   start_date: str = None, datalen: int = 10000) -> pd.DataFrame:
    """从 Tushare 获取K线数据（1分~月线，数据质量最高，需 token）。

    需要 TUSHARE_TOKEN 环境变量。如未设置则跳过。

    Returns:
        DataFrame with columns: Date, Open, High, Low, Close, Volume.
        Empty DataFrame if token not configured or Tushare not installed.
    """
    token = os.environ.get("TUSHARE_TOKEN", "")
    if not token:
        return pd.DataFrame()

    try:
        import tushare as ts
    except ImportError:
        logger.info("tushare not installed, skipping Tushare data source")
        return pd.DataFrame()

    ts.set_token(token)
    pro = ts.pro_api()

    _TS_FREQ = {
        "1min": "1min", "5min": "5min", "15min": "15min",
        "30min": "30min", "60min": "60min",
        "daily": "daily", "weekly": "weekly", "monthly": "monthly",
    }
    freq = _TS_FREQ.get(level, "daily")

    # Tushare 需要 SH/SZ 前缀
    ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"

    # start_date for tushare format
    ts_start = None
    if start_date:
        ts_start = start_date.replace("-", "")

    try:
        if freq in ("1min", "5min", "15min", "30min", "60min"):
            df = pro.stk_mins(
                ts_code=ts_code, freq=freq, start_date=ts_start, limit=datalen
            )
        else:
            df = pro.daily(
                ts_code=ts_code, start_date=ts_start, limit=datalen
            ) if freq == "daily" else pro.weekly(
                ts_code=ts_code, start_date=ts_start, limit=datalen
            ) if freq == "weekly" else pro.monthly(
                ts_code=ts_code, start_date=ts_start, limit=datalen
            )
    except Exception as e:
        logger.warning("Tushare fetch failed for %s: %s", code, e)
        return pd.DataFrame()

    if df is None or df.empty:
        return pd.DataFrame()

    # Tushare columns: trade_date, open, high, low, close, vol
    rename_map = {
        "trade_date": "Date",
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "vol": "Volume",
    }
    df = df.rename(columns=rename_map)
    df = df[["Date", "Open", "High", "Low", "Close", "Volume"]]
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").reset_index(drop=True)

    if start_date:
        df = df[df["Date"] >= pd.to_datetime(start_date)]

    return df


def _tickflow_kline(code: str, level: str = "daily",
                    start_date: str = None, datalen: int = 10000) -> pd.DataFrame:
    """从 TickFlow 获取K线数据（1分~月线，需 API key）。

    需要 SYSTEM_TICKFLOW_API_KEY 环境变量。如未设置则跳过。

    Returns:
        DataFrame with columns: Date, Open, High, Low, Close, Volume.
        Empty DataFrame if API key not configured or TickFlow not installed.
    """
    apikey = os.environ.get("SYSTEM_TICKFLOW_API_KEY", "")
    if not apikey:
        return pd.DataFrame()

    try:
        from tickflow import TickFlow
    except ImportError:
        logger.info("tickflow not installed, skipping TickFlow data source")
        return pd.DataFrame()

    # Convert 6-digit code to TickFlow symbol format
    if code.startswith(("6", "9")):
        symbol = f"{code}.SH"
    elif code.startswith(("0", "3")):
        symbol = f"{code}.SZ"
    elif code.startswith(("8", "4")):
        symbol = f"{code}.BJ"
    else:
        symbol = f"{code}.SZ"

    # Map internal level to TickFlow period
    _TF_PERIOD = {
        "1min": "1m", "5min": "5m", "15min": "15m", "30min": "30m",
        "60min": "60m", "daily": "1d", "weekly": "1w", "monthly": "1M",
    }
    period = _TF_PERIOD.get(level, "1d")

    try:
        tf = TickFlow(api_key=apikey)
        kwargs = dict(symbol=symbol, period=period, count=datalen,
                      adjust="forward_additive", as_dataframe=True)
        if start_date:
            start_dt = pd.to_datetime(start_date)
            kwargs["start_time"] = int(start_dt.timestamp() * 1000)
        df = tf.klines.get(**kwargs)
    except Exception as e:
        logger.warning("TickFlow fetch failed for %s: %s", code, e)
        return pd.DataFrame()

    if df is None or df.empty:
        return pd.DataFrame()

    # Convert DataFrame columns
    rename_map = {
        "trade_date": "Date",
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    }
    df = df.rename(columns=rename_map)
    df = df[["Date", "Open", "High", "Low", "Close", "Volume"]]
    df["Date"] = pd.to_datetime(df["Date"])

    if start_date:
        df = df[df["Date"] >= pd.to_datetime(start_date)]

    return df


def _ths_kline_5min(code: str, start_date: str = None,
                    datalen: int = 10000) -> pd.DataFrame:
    """从同花顺获取5分钟K线数据（用于聚合为更大级别K线）。

    同花顺 d.10jqka.com.cn 接口。
    每条格式: timestamp,open,high,low,close,volume,amount,change%,,,flag
    timestamp 格式: YYYYMMDDHHmm

    注意: 同花顺 period 代码不是时间级别，而是数据类型代码:
      01 = 日线, 30 = 5分钟线, 60 = 1分钟线

    部分股票5分钟K线(period=30)缺数据时，降级到1分钟K线(period=60)
    再聚合为5分钟K线。

    Returns:
        DataFrame with columns: Date, Open, High, Low, Close, Volume.
    """
    headers = {
        "User-Agent": _UA,
        "Referer": "https://d.10jqka.com.cn/",
    }

    # 确定需要哪些年份的数据
    import datetime as _dt
    start_year = (pd.to_datetime(start_date) if start_date else _dt.datetime(2024, 1, 1)).year
    end_year = _dt.datetime.now().year

    def _fetch_ths_year(year: int, period: str) -> pd.DataFrame:
        """获取指定年份的K线数据。"""
        url = f"https://d.10jqka.com.cn/v6/line/hs_{code}/{period}/{year}.js"
        try:
            r = _requests.get(url, headers=headers, timeout=15, proxies=_NO_PROXY)
            r.raise_for_status()
        except Exception:
            # SSL fallback
            try:
                r = _requests.get(url, headers=headers, timeout=15,
                                  verify=False, proxies=_NO_PROXY)
                r.raise_for_status()
            except Exception:
                return pd.DataFrame()

        text = r.text.strip()
        # JSONP: quotebridge_v6_line_hs_{code}_{period}_{year}({...})
        eq_pos = text.find("(")
        if eq_pos < 0:
            return pd.DataFrame()
        json_str = text[eq_pos + 1:].rstrip(")")
        try:
            d = _json.loads(json_str)
        except (ValueError, _json.JSONDecodeError):
            return pd.DataFrame()

        data_str = d.get("data", "")
        if not data_str:
            return pd.DataFrame()

        # 解析: timestamp,open,high,low,close,volume,amount,change,...;...
        rows = []
        for segment in data_str.split(";"):
            segment = segment.strip()
            if not segment:
                continue
            parts = segment.split(",")
            if len(parts) < 6:
                continue
            try:
                ts = parts[0]
                # YYYYMMDDHHmm → datetime
                dt = pd.to_datetime(ts, format="%Y%m%d%H%M")
                rows.append({
                    "Date": dt,
                    "Open": float(parts[1]),
                    "High": float(parts[2]),
                    "Low": float(parts[3]),
                    "Close": float(parts[4]),
                    "Volume": int(float(parts[5])),
                })
            except (ValueError, IndexError):
                continue

        return pd.DataFrame(rows) if rows else pd.DataFrame()

    # 先尝试5分钟K线 (period=30)
    all_rows_df = pd.DataFrame()
    for year in range(start_year, end_year + 1):
        df_year = _fetch_ths_year(year, "30")
        if not df_year.empty:
            all_rows_df = pd.concat([all_rows_df, df_year], ignore_index=True)
        # 同花顺限流：年份间加延时
        _time.sleep(0.3)

    # 如果5分钟数据为空，降级到1分钟K线 (period=60)，再聚合为5分钟
    if all_rows_df.empty:
        logger.info("THS 5min kline empty for %s, falling back to 1min→5min", code)
        for year in range(start_year, end_year + 1):
            df_year = _fetch_ths_year(year, "60")
            if not df_year.empty:
                all_rows_df = pd.concat([all_rows_df, df_year], ignore_index=True)
            _time.sleep(0.3)

        if not all_rows_df.empty:
            all_rows_df = all_rows_df.sort_values("Date").reset_index(drop=True)
            # 1分钟→5分钟聚合
            all_rows_df = _resample_minute_kline(all_rows_df, "5min")

    if all_rows_df.empty:
        return pd.DataFrame()

    all_rows_df = all_rows_df.sort_values("Date").reset_index(drop=True)

    if start_date:
        all_rows_df = all_rows_df[all_rows_df["Date"] >= pd.to_datetime(start_date)]

    # 限制数据量
    if len(all_rows_df) > datalen:
        all_rows_df = all_rows_df.iloc[-datalen:]

    return all_rows_df


def _resample_minute_kline(df_5min: pd.DataFrame,
                           target_level: str) -> pd.DataFrame:
    """将5分钟K线聚合为更大时间级别的K线。

    聚合规则:
        5min: 每1根1min → 1根5min (1min→5min转换)
        15min: 每3根5min → 1根15min
        30min: 每6根5min → 1根30min
        60min: 每12根5min → 1根60min

    A 股交易时段: 9:30-11:30, 13:00-15:00
    聚合边界必须对齐交易时段:
        30min: [9:30-10:00] [10:00-10:30] [10:30-11:00] [11:00-11:30]
               [13:00-13:30] [13:30-14:00] [14:00-14:30] [14:30-15:00]

    Args:
        df_5min: 5分钟(或1分钟)K线 DataFrame
        target_level: 目标级别 (5min/15min/30min/60min)

    Returns:
        聚合后的 DataFrame
    """

    _LEVEL_MINUTES = {"5min": 5, "15min": 15, "30min": 30, "60min": 60}
    target_min = _LEVEL_MINUTES.get(target_level)
    if target_min is None:
        return pd.DataFrame()

    # 自动检测输入K线的基础粒度（5min或1min）
    if len(df_5min) >= 2:
        delta = (df_5min["Date"].iloc[1] - df_5min["Date"].iloc[0]).total_seconds()
        base_minutes = max(int(delta / 60), 1)
    else:
        base_minutes = 5  # 默认5min

    ratio = max(target_min // base_minutes, 1)  # e.g. 30/5=6, 30/1=30, 5/1=5

    # 为每条5min K线计算所属的30min区间起始时间
    # A 股交易时段对齐
    def _align_period(dt, period_minutes):
        """将 datetime 对齐到最近的 K 线周期起始时间。"""
        h, m = dt.hour, dt.minute
        # 午休分隔：13:00 开始新半天
        if h >= 13:
            # 下午盘: 从 13:00 开始算
            afternoon_min = (h - 13) * 60 + m
            period_start_min = (afternoon_min // period_minutes) * period_minutes
            ph = 13 + period_start_min // 60
            pm = period_start_min % 60
        else:
            # 上午盘: 从 9:30 开始算
            morning_min = (h - 9) * 60 + m - 30
            if morning_min < 0:
                morning_min = 0
            period_start_min = (morning_min // period_minutes) * period_minutes
            ph = 9 + (period_start_min + 30) // 60
            pm = (period_start_min + 30) % 60
        return dt.replace(hour=ph, minute=pm, second=0, microsecond=0)

    # 按日期 + 对齐后的区间分组
    df_5min = df_5min.copy()
    df_5min["_period"] = df_5min["Date"].apply(
        lambda dt: _align_period(dt, target_min)
    )

    # 聚合
    agg_df = df_5min.groupby("_period").agg(
        Date=("Date", "first"),      # 区间内第一根的时间
        Open=("Open", "first"),
        High=("High", "max"),
        Low=("Low", "min"),
        Close=("Close", "last"),
        Volume=("Volume", "sum"),
    ).reset_index(drop=True)

    # 只保留完整周期（K线数量等于 ratio）
    # 放宽条件：至少有 ratio-2 根才认为是合理区间（尾盘可能不完整）
    period_counts = df_5min.groupby("_period").size()
    valid_periods = period_counts[period_counts >= max(ratio - 2, 2)].index
    agg_df = agg_df[agg_df["Date"].isin(
        df_5min[df_5min["_period"].isin(valid_periods)]["Date"]
    )].reset_index(drop=True)

    return agg_df[["Date", "Open", "High", "Low", "Close", "Volume"]]




def _eastmoney_trends2(code: str, level: str = "30min",
                       ndays: int = 5) -> pd.DataFrame:
    """从东财 trends2 API 获取分时走势数据并聚合为分钟K线。

    push2his/push2 K线接口被代理阻断时，trends2 是可靠备选。
    trends2 返回最近 ndays 天的1分钟分时数据。

    Args:
        code: 6位股票代码
        level: 目标K线级别 (5min/15min/30min/60min)
        ndays: 获取最近多少天的分时数据 (最多5天有效)

    Returns:
        DataFrame with columns: Date, Open, High, Low, Close, Volume
    """
    secid = _eastmoney_secid(code)
    url = "http://push2his.eastmoney.com/api/qt/stock/trends2/get"
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "secid": secid,
        "ndays": str(min(ndays, 5)),
    }
    try:
        r = _requests.get(url, params=params, headers={"User-Agent": _UA},
                          timeout=15, proxies=_NO_PROXY)
        data = r.json()
    except Exception as e:
        logger.warning("trends2 failed for %s: %s", code, e)
        return pd.DataFrame()

    trends = (data or {}).get("data", {}).get("trends", [])
    if not trends:
        return pd.DataFrame()

    # trends 格式: "2025-04-18 09:30,0.37,0.37,0.37,0.37,30729,1136973.00"
    rows = []
    for t in trends:
        parts = t.split(",")
        if len(parts) < 7:
            continue
        try:
            rows.append({
                "Date": pd.to_datetime(parts[0]),
                "Open": float(parts[1]),
                "Close": float(parts[2]),
                "High": float(parts[3]),
                "Low": float(parts[4]),
                "Volume": int(float(parts[5])),
            })
        except (ValueError, IndexError):
            continue

    if not rows:
        return pd.DataFrame()

    df_1min = pd.DataFrame(rows)
    # 聚合为目标级别
    df_resampled = _resample_minute_kline(df_1min, level)
    return df_resampled


def _fetch_kline(code: str, level: str = "daily",
                 start_date: str = None, datalen: int = 10000,
                 sources: list[str] | None = None) -> tuple[pd.DataFrame, str]:
    """多数据源K线获取，主源失败自动切换备源。

    Args:
        code: 6位股票代码
        level: K线级别 (1min/5min/15min/30min/60min/daily/weekly/monthly)
        start_date: 起始日期 (YYYY-MM-DD)，默认从配置中读取
        datalen: 最大K线数量
        sources: 指定数据源顺序，None则按级别自动选择

    Returns:
        (DataFrame, source_name) 元组。DataFrame 列: Date,Open,High,Low,Close,Volume
    """
    if not start_date:
        from .config import get_config
        config = get_config()
        start_date = config.get("kline_start_date", "2024-01-01")

    if sources is None:
        if level in ("1min", "5min", "15min", "30min", "60min"):
            sources = _KLINE_FALLBACK_MINUTE
        else:
            sources = _KLINE_FALLBACK_DAILY

    source_funcs = {
        "eastmoney": _eastmoney_kline,
        "sina": _sina_kline,
        "tencent": _tencent_kline,
        "tushare": _tushare_kline,
        "tickflow": _tickflow_kline,
    }

    errors: list[str] = []
    for src in sources:
        func = source_funcs.get(src)
        if func is None:
            continue
        try:
            df = func(code, level=level, start_date=start_date, datalen=datalen)
            if df is not None and not df.empty:
                # 周线/月线：如果不是直接获取的级别，尝试从日线重采样
                return df, src
        except Exception as e:
            errors.append(f"{src}: {e}")
            logger.warning("K-line source %s failed for %s/%s: %s", src, code, level, e)
            # 请求间加延时，避免连续请求触发限流
            _time.sleep(_KLINE_REQUEST_INTERVAL)
            continue

    # 分钟级K线: 依次尝试同花顺5分钟聚合 → 东财 trends2 (最近5天分时, 最后兜底)
    if level in ("5min", "15min", "30min", "60min"):
        # 1) 同花顺5分钟K线聚合（历史数据丰富）
        try:
            df_5min = _ths_kline_5min(code, start_date=start_date, datalen=datalen * 6)
            if df_5min is not None and not df_5min.empty:
                if level == "5min":
                    return df_5min, "ths5min"
                df_resampled = _resample_minute_kline(df_5min, level)
                if df_resampled is not None and not df_resampled.empty:
                    return df_resampled, f"ths5min(→{level})"
        except Exception as e:
            errors.append(f"ths5min: {e}")

        # 2) 东财 trends2: 从分时走势获取近5天分钟数据并聚合（兜底，仅近期数据）
        try:
            df_t2 = _eastmoney_trends2(code, level=level, ndays=5)
            if df_t2 is not None and not df_t2.empty:
                return df_t2, f"trends2(→{level})"
        except Exception as e:
            errors.append(f"trends2: {e}")

    # 所有源都失败了 — 日线 fallback from mootdx (TCP)
    if level in ("daily", "weekly", "monthly"):
        try:
            client = _get_mootdx_client()
            cat_map = {"daily": 4, "weekly": 5, "monthly": 6}
            raw = client.bars(symbol=code, category=cat_map.get(level, 4), offset=800)
            if raw is not None and not raw.empty:
                raw = raw.drop(
                    columns=["datetime", "year", "month", "day", "hour", "minute"],
                    errors="ignore",
                )
                raw = raw.reset_index()
                raw = raw.rename(columns={
                    "datetime": "Date", "open": "Open", "close": "Close",
                    "high": "High", "low": "Low", "volume": "Volume",
                })
                df = raw[["Date", "Open", "High", "Low", "Close", "Volume"]]
                df["Date"] = pd.to_datetime(df["Date"])
                if start_date:
                    df = df[df["Date"] >= pd.to_datetime(start_date)]
                if not df.empty:
                    return df, "mootdx"
        except Exception as e:
            errors.append(f"mootdx: {e}")

    logger.error("All K-line sources failed for %s/%s: %s", code, level, errors)
    return pd.DataFrame(), "none"


def _resample_kline(df: pd.DataFrame, target_level: str) -> pd.DataFrame:
    """将K线数据重采样为更高级别（日线→周线/月线，或分钟级→30分钟等）。

    Args:
        df: 源 DataFrame，需包含 Date/Open/High/Low/Close/Volume 列
        target_level: 目标级别: "30min"/"60min"/"weekly" 或 "monthly"

    Returns:
        重采样后的 DataFrame
    """
    if df.empty:
        return df

    # 分钟级重采样
    _MINUTE_RESAMPLE = {
        "30min": "30min",
        "60min": "60min",
    }
    if target_level in _MINUTE_RESAMPLE:
        df = df.copy()
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date")
        rule = _MINUTE_RESAMPLE[target_level]
        agg = {
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }
        resampled = df.resample(rule).agg(agg).dropna()
        return resampled.reset_index()

    # 日线 → 周线/月线
    if target_level not in ("weekly", "monthly"):
        return df

    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date")

    rule = "W-MON" if target_level == "weekly" else "ME"
    agg = {
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }
    resampled = df.resample(rule).agg(agg).dropna()
    resampled = resampled.reset_index()

    return resampled


# ---------------------------------------------------------------------------
# Legacy helpers (kept for backward compat)
# ---------------------------------------------------------------------------


def _sina_kline_fallback(code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """Fetch daily K-line from Sina HTTP API as mootdx fallback.

    Returns DataFrame with columns: Date, Open, High, Low, Close, Volume.
    """
    df = _sina_kline(code, level="daily", start_date=start_date, datalen=800)
    if end_date and not df.empty:
        df = df[df["Date"] <= pd.to_datetime(end_date)]
    return df


def _sina_kline_full(code: str, datalen: int = 5000) -> pd.DataFrame:
    """从新浪获取全量日线K线数据（最多5000根，覆盖上市至今）。

    与 _sina_kline_fallback 的区别：
    - datalen 参数化，默认5000（fallback写死800）
    - 返回完整 DataFrame，不做日期过滤
    - 用于一次性全量同步 + 本地缓存

    Returns:
        DataFrame with columns: Date, Open, High, Low, Close, Volume.
    """
    return _sina_kline(code, level="daily", start_date=None, datalen=datalen)


# ---------------------------------------------------------------------------
# OHLCV loading with cache (mootdx -> CSV)
# ---------------------------------------------------------------------------

def _cache_dir() -> str:
    """Return the data cache directory, creating it if needed."""
    from .config import get_config
    config = get_config()
    cache_dir = config.get(
        "data_cache_dir", os.path.expanduser("~/.tradingagents/cache")
    )
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


# ---------------------------------------------------------------------------
#  SQLite-backed cache (replaces CSV + JSON)
# ---------------------------------------------------------------------------

from .kline_cache import (  # noqa: E402
    load_kline as _db_load,
    save_kline as _db_save,
    incremental_update as _db_incremental,
    get_latest_date as _db_latest_date,
    get_meta as _db_get_meta,
    list_cached as _db_list_cached,
    delete_kline as _db_delete,
    migrate_csv_to_sqlite as _db_migrate,
)


def _daily_cache_path(code: str, level: str = "daily") -> str:
    """Return the K-line CSV path for a stock code and level (legacy compat)."""
    return os.path.join(_cache_dir(), f"{code}-astock-{level}.csv")


def _daily_meta_path(code: str, level: str = "daily") -> str:
    """Return the K-line metadata JSON path (legacy compat)."""
    return os.path.join(_cache_dir(), f"{code}-astock-{level}-meta.json")


def sync_stock_data(code: str, level: str = "daily",
                    datalen: int = 10000, start_date: str = None,
                    sources: list[str] | None = None) -> dict:
    """同步单只股票的K线数据到本地 SQLite 缓存。增量更新：优先用缓存，仅拉取新增部分。

    同步策略:
        1. 查询本地 SQLite 缓存最新日期
        2. 如有缓存且数据较新（当天或上一交易日），直接返回缓存数据
        3. 如缓存不是最新，仅拉取最新日期之后的数据（增量），合并写入
        4. 无缓存则全量拉取
        5. 主源失败自动切换备源（东财→新浪→腾讯→Tushare→同花顺5min聚合→东财trends2→mootdx TCP）

    Args:
        code: 6位股票代码
        level: K线级别 (1min/5min/15min/30min/60min/daily/weekly/monthly)
        datalen: 拉取的最大K线数量，默认10000
        start_date: 起始日期 (YYYY-MM-DD)，None则从配置读取 (默认20240101)
        sources: 指定数据源顺序，None则按级别自动选择

    Returns:
        同步结果 dict:
        {
            "code": "300750",
            "level": "daily",
            "rows": 3200,
            "date_range": ["2008-06-12", "2025-06-15"],
            "source": "sina",
            "status": "ok" | "error",
            "error": None | "error message",
            "incremental": True | False   # 是否为增量更新
        }
    """
    code = _normalize_ticker(code)

    # Resolve start_date
    if not start_date:
        from .config import get_config
        config = get_config()
        start_date = config.get("kline_start_date", "2024-01-01")

    # ── Step 1: Check SQLite cache — 增量更新 ──
    cached_latest = _db_latest_date(code, level, "stock")
    is_minute = level.endswith("min")
    now = datetime.now()

    if cached_latest:
        cache_dt = pd.to_datetime(cached_latest)
        if is_minute:
            # 分钟级：缓存最新时间在1小时内视为最新（盘中实时）
            is_fresh = (now - cache_dt).total_seconds() < 3600
        else:
            # 日线级别：缓存最新日期是今天或昨天（含周末/节假日），视为最新
            is_fresh = (now.date() - cache_dt.date()).days <= 1

        if is_fresh:
            # 缓存已是最新，直接返回
            meta = _db_get_meta(code, level, "stock")
            if meta and meta.get("rows", 0) > 0:
                logger.info("Cache fresh for %s/%s (latest=%s, rows=%d), skip fetch",
                            code, level, cached_latest, meta["rows"])
                result = dict(meta)
                result["status"] = "ok"
                result["error"] = None
                result["incremental"] = False
                return result

        # 缓存过期但有旧数据 → 增量拉取
        # 用缓存最新日期 +1 天作为增量起始
        if is_minute:
            inc_start = (cache_dt + pd.Timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
        else:
            inc_start = (cache_dt + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        logger.info("Incremental update for %s/%s: cached latest=%s, fetching from %s",
                    code, level, cached_latest, inc_start)
        df, source_name = _fetch_kline(
            code, level=level, start_date=inc_start,
            datalen=datalen, sources=sources,
        )

        if df.empty:
            # 增量获取为空 → 可能无新交易日，返回缓存
            meta = _db_get_meta(code, level, "stock")
            if meta and meta.get("rows", 0) > 0:
                result = dict(meta)
                result["status"] = "ok"
                result["error"] = None
                result["incremental"] = False
                logger.info("No new data for %s/%s, returning cache", code, level)
                return result

        if not df.empty:
            # 增量合并写入 SQLite
            result = _db_incremental(df, code, level, data_type="stock",
                                     source=source_name, start_date=start_date)
            result["incremental"] = True
            return result

    # ── Step 2: 全量拉取（无缓存或缓存不可用）──
    df, source_name = _fetch_kline(
        code, level=level, start_date=start_date,
        datalen=datalen, sources=sources,
    )

    # 周线/月线：尝试直接获取失败后，从日线重采样
    if df.empty and level in ("weekly", "monthly"):
        logger.info("Direct %s fetch failed for %s, trying daily resample", level, code)
        _time.sleep(_KLINE_REQUEST_INTERVAL)  # 间隔避免限流
        df_daily, daily_src = _fetch_kline(
            code, level="daily", start_date=start_date,
            datalen=datalen, sources=sources,
        )
        if not df_daily.empty:
            df = _resample_kline(df_daily, level)
            source_name = f"{daily_src}(resample→{level})"

    # mootdx TCP fallback for daily
    if df.empty and level == "daily":
        try:
            client = _get_mootdx_client()
            raw = client.bars(symbol=code, category=4, offset=800)
            if raw is not None and not raw.empty:
                raw = raw.drop(
                    columns=["datetime", "year", "month", "day", "hour", "minute"],
                    errors="ignore",
                )
                raw = raw.reset_index()
                raw = raw.rename(columns={
                    "datetime": "Date", "open": "Open", "close": "Close",
                    "high": "High", "low": "Low", "volume": "Volume",
                })
                df = raw[["Date", "Open", "High", "Low", "Close", "Volume"]]
                df["Date"] = pd.to_datetime(df["Date"])
                source_name = "mootdx"
        except Exception:
            pass

    if df.empty:
        return {
            "code": code, "level": level, "rows": 0,
            "date_range": [], "source": "none",
            "status": "error", "error": "所有数据源均无法获取数据",
            "incremental": False,
        }

    # Save to SQLite
    result = _db_save(df, code, level, data_type="stock",
                      source=source_name, start_date=start_date)
    result["incremental"] = False
    return result


def get_cached_stocks() -> list[dict]:
    """列出所有已缓存的股票及其元信息。

    Returns:
        [{"code": "300750", "level": "daily", "rows": 3200, ...}, ...]
    """
    return _db_list_cached(data_type="stock")


def delete_cache(code: str, level: str | None = None) -> bool:
    """删除指定股票的缓存数据。

    Args:
        code: 6位股票代码
        level: K线级别，None则删除所有级别

    Returns:
        True if any record was deleted, False otherwise.
    """
    return _db_delete(code, level=level, data_type="stock")


# ---------------------------------------------------------------------------
#  Sector / Board data sync (板块数据同步)
# ---------------------------------------------------------------------------

def _sector_cache_path(sector_code: str, level: str = "daily") -> str:
    """Return the K-line CSV path for a sector code and level."""
    return os.path.join(_cache_dir(), f"{sector_code}-sector-{level}.csv")


def _sector_meta_path(sector_code: str, level: str = "daily") -> str:
    """Return the sector metadata JSON path."""
    return os.path.join(_cache_dir(), f"{sector_code}-sector-{level}-meta.json")


def get_sector_list(sector_type: str = "industry") -> list[dict]:
    """获取东方财富行业/概念板块列表。

    Args:
        sector_type: "industry" (行业板块) 或 "concept" (概念板块)

    Returns:
        list of {"code": "BK0428", "name": "酿酒行业", "change_pct": 1.23, ...}
    """
    fs_map = {"industry": "m:90+t:2", "concept": "m:90+t:3"}
    fs = fs_map.get(sector_type, "m:90+t:2")

    url = "http://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1",
        "pz": "500",
        "po": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fs": fs,
        "fields": "f2,f3,f4,f12,f13,f14,f104,f105,f128,f136,f140,f141",
    }

    results = []
    page = 1
    while True:
        params["pn"] = str(page)
        try:
            r = _requests.get(url, params=params, headers={"User-Agent": _UA}, timeout=15,
                              proxies=_NO_PROXY)
            r.raise_for_status()
        except Exception:
            try:
                r = _requests.get(url, params=params, headers={"User-Agent": _UA},
                                  timeout=15, verify=False, proxies=_NO_PROXY)
                r.raise_for_status()
            except Exception:
                break
        d = r.json()
        items = d.get("data", {}).get("diff", [])
        if not items:
            break
        for item in items:
            code = item.get("f12", "")
            name = item.get("f14", "")
            if not code or not name:
                continue
            results.append({
                "code": code,
                "name": name,
                "change_pct": item.get("f3", 0),
                "price": item.get("f2", 0),
                "up_count": item.get("f104", 0),
                "down_count": item.get("f105", 0),
                "leader": item.get("f140", ""),
            })
        total = d.get("data", {}).get("total", 0)
        if page * 500 >= total:
            break
        page += 1
    return results


def _eastmoney_sector_kline(
    sector_code: str, level: str = "daily",
    start_date: str = None, datalen: int = 10000,
) -> pd.DataFrame:
    """从东方财富获取板块K线数据。

    板块secid格式: 90.{code} (如 90.BK0428)
    板块指数不需要复权 (fqt=0)
    """
    secid = f"90.{sector_code}"
    klt = _EASTMONEY_KLT.get(level, 101)

    url = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid,
        "klt": str(klt),
        "fqt": "0",  # 板块指数不需要复权
        "lmt": str(datalen),
        "end": "20500101",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
    }

    try:
        r = _requests.get(url, params=params, headers={"User-Agent": _UA}, timeout=30,
                          proxies=_NO_PROXY)
        r.raise_for_status()
    except Exception as e:
        try:
            r = _requests.get(url, params=params, headers={"User-Agent": _UA},
                              timeout=30, verify=False, proxies=_NO_PROXY)
            r.raise_for_status()
        except Exception:
            logger.warning("Eastmoney sector kline failed for %s/%s: %s", sector_code, level, e)
            return pd.DataFrame()

    d = r.json()
    klines = d.get("data", {}).get("klines", [])
    if not klines:
        return pd.DataFrame()

    rows = []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 7:
            continue
        rows.append({
            "Date": parts[0],
            "Open": float(parts[1]),
            "Close": float(parts[2]),
            "High": float(parts[3]),
            "Low": float(parts[4]),
            "Volume": int(float(parts[5])),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["Date"] = pd.to_datetime(df["Date"])

    if start_date:
        df = df[df["Date"] >= pd.to_datetime(start_date)]

    return df


def sync_sector_data(
    sector_code: str,
    sector_name: str = "",
    level: str = "daily",
    datalen: int = 10000,
    start_date: str = None,
) -> dict:
    """同步板块K线数据到本地 SQLite 缓存。支持增量更新。

    Args:
        sector_code: 板块代码 (如 BK0428)
        sector_name: 板块名称 (如 酿酒行业)，仅用于日志
        level: K线级别
        datalen: 最大K线数量
        start_date: 起始日期

    Returns:
        同步结果 dict (与 sync_stock_data 格式一致)
    """
    if not start_date:
        from .config import get_config
        config = get_config()
        start_date = config.get("kline_start_date", "2024-01-01")

    # ── 增量检查 ──
    cached_latest = _db_latest_date(sector_code, level, "sector")
    is_minute = level.endswith("min")
    now = datetime.now()

    if cached_latest:
        cache_dt = pd.to_datetime(cached_latest)
        if is_minute:
            is_fresh = (now - cache_dt).total_seconds() < 3600
        else:
            is_fresh = (now.date() - cache_dt.date()).days <= 1

        if is_fresh:
            meta = _db_get_meta(sector_code, level, "sector")
            if meta and meta.get("rows", 0) > 0:
                meta["status"] = "ok"
                meta["error"] = None
                meta["name"] = sector_name or meta.get("name", "")
                meta["incremental"] = False
                return meta

        # 增量拉取
        if is_minute:
            inc_start = (cache_dt + pd.Timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
        else:
            inc_start = (cache_dt + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        df = _eastmoney_sector_kline(
            sector_code, level=level, start_date=inc_start, datalen=datalen,
        )
        if not df.empty:
            result = _db_incremental(df, sector_code, level, data_type="sector",
                                     source="eastmoney", start_date=start_date,
                                     name=sector_name)
            result["incremental"] = True
            return result
        # 增量为空 → 返回缓存
        meta = _db_get_meta(sector_code, level, "sector")
        if meta and meta.get("rows", 0) > 0:
            meta["status"] = "ok"
            meta["error"] = None
            meta["incremental"] = False
            return meta

    # ── 全量拉取 ──
    df = _eastmoney_sector_kline(
        sector_code, level=level, start_date=start_date, datalen=datalen,
    )

    if df.empty:
        return {
            "code": sector_code, "level": level, "rows": 0,
            "date_range": [], "source": "none",
            "status": "error", "error": "东方财富无法获取该板块K线数据",
        }

    result = _db_save(df, sector_code, level, data_type="sector",
                      source="eastmoney", start_date=start_date,
                      name=sector_name)
    result["incremental"] = False
    return result


def get_cached_sectors() -> list[dict]:
    """列出所有已缓存的板块数据。"""
    return _db_list_cached(data_type="sector")


def delete_sector_cache(sector_code: str, level: str | None = None) -> bool:
    """删除指定板块的缓存数据。"""
    return _db_delete(sector_code, level=level, data_type="sector")


# ---------------------------------------------------------------------------
# Index data sync (指数K线)
# ---------------------------------------------------------------------------

def _index_cache_path(index_code: str, level: str = "daily") -> str:
    """Return the K-line CSV path for an index code and level."""
    return os.path.join(_cache_dir(), f"{index_code}-index-{level}.csv")


def _index_meta_path(index_code: str, level: str = "daily") -> str:
    """Return the index metadata JSON path."""
    return os.path.join(_cache_dir(), f"{index_code}-index-{level}-meta.json")


def get_index_list() -> list[dict]:
    """获取预置的主要指数列表。

    Returns:
        list of {"code": "000001", "name": "上证指数", "market": 1, "type": "index"}
    """
    return INDEX_SECTORS.copy()


def sync_index_data(
    index_code: str,
    index_name: str = "",
    level: str = "daily",
    datalen: int = 10000,
    start_date: str = None,
) -> dict:
    """同步指数K线数据到本地 SQLite 缓存。支持增量更新。

    Args:
        index_code: 指数代码 (如 000001, 399001)
        index_name: 指数名称 (如 上证指数)
        level: K线级别
        datalen: 最大K线数量
        start_date: 起始日期

    Returns:
        同步结果 dict
    """
    if not start_date:
        from .config import get_config
        config = get_config()
        start_date = config.get("kline_start_date", "2024-01-01")

    # ── 增量检查 ──
    cached_latest = _db_latest_date(index_code, level, "index")
    is_minute = level.endswith("min")
    now = datetime.now()

    if cached_latest:
        cache_dt = pd.to_datetime(cached_latest)
        if is_minute:
            is_fresh = (now - cache_dt).total_seconds() < 3600
        else:
            is_fresh = (now.date() - cache_dt.date()).days <= 1

        if is_fresh:
            meta = _db_get_meta(index_code, level, "index")
            if meta and meta.get("rows", 0) > 0:
                meta["status"] = "ok"
                meta["error"] = None
                meta["name"] = index_name or meta.get("name", "")
                meta["incremental"] = False
                return meta

        # 增量拉取
        if is_minute:
            inc_start = (cache_dt + pd.Timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
        else:
            inc_start = (cache_dt + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        df, src = _fetch_kline(index_code, level=level, start_date=inc_start, datalen=datalen)
        if not df.empty:
            result = _db_incremental(df, index_code, level, data_type="index",
                                     source=src, start_date=start_date,
                                     name=index_name)
            result["incremental"] = True
            return result
        # 增量为空 → 返回缓存
        meta = _db_get_meta(index_code, level, "index")
        if meta and meta.get("rows", 0) > 0:
            meta["status"] = "ok"
            meta["error"] = None
            meta["incremental"] = False
            return meta

    # ── 全量拉取 ──
    df, source_name = _fetch_kline(
        index_code, level=level, start_date=start_date, datalen=datalen,
    )

    # 周线/月线：尝试从日线重采样
    if df.empty and level in ("weekly", "monthly"):
        _time.sleep(_KLINE_REQUEST_INTERVAL)
        df_daily, daily_src = _fetch_kline(
            index_code, level="daily", start_date=start_date, datalen=datalen,
        )
        if not df_daily.empty:
            df = _resample_kline(df_daily, level)
            source_name = f"{daily_src}(resample→{level})"

    if df.empty:
        return {
            "code": index_code, "name": index_name, "level": level, "rows": 0,
            "date_range": [], "source": "none",
            "status": "error", "error": "所有数据源均无法获取该指数K线数据",
        }

    result = _db_save(df, index_code, level, data_type="index",
                      source=source_name, start_date=start_date,
                      name=index_name)
    result["name"] = index_name
    result["incremental"] = False
    return result


def get_cached_indices() -> list[dict]:
    """列出所有已缓存的指数数据。"""
    return _db_list_cached(data_type="index")


def delete_index_cache(index_code: str, level: str | None = None) -> bool:
    """删除指定指数的缓存数据。"""
    return _db_delete(index_code, level=level, data_type="index")


def _load_ohlcv_astock(symbol: str, curr_date: str) -> pd.DataFrame:
    """Fetch OHLCV daily data, using SQLite cache first, then multi-source fetch.

    1. Check SQLite cache — if data exists and is fresh (≤1 day old), use it directly
    2. If cache is stale, do an incremental update (only fetch new bars)
    3. If no cache, full fetch from multi-source API
    4. Always filter by curr_date to prevent look-ahead bias
    """
    code = _normalize_ticker(symbol)

    # 优先从 SQLite 缓存加载
    cached_latest = _db_latest_date(code, "daily", "stock")

    if cached_latest:
        cache_dt = pd.to_datetime(cached_latest)
        now = datetime.now()
        # 日线：缓存最新日期是今天或昨天，视为最新
        is_fresh = (now.date() - cache_dt.date()).days <= 1

        if not is_fresh:
            # 缓存过期 → 增量更新
            inc_start = (cache_dt + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            logger.info("Incremental OHLCV update for %s: cached latest=%s, fetching from %s",
                        code, cached_latest, inc_start)
            try:
                df_inc, src = _fetch_kline(code, level="daily", start_date=inc_start, datalen=10000)
                if not df_inc.empty:
                    _db_incremental(df_inc, code, "daily", data_type="stock", source=src)
            except Exception as e:
                logger.warning("Incremental update failed for %s: %s, using stale cache", code, e)

        # 从 SQLite 加载全部数据
        data = _db_load(code, "daily", data_type="stock")
        if not data.empty:
            data["Date"] = pd.to_datetime(data["Date"])
            cutoff = pd.to_datetime(curr_date)
            return data[data["Date"] <= cutoff]

    # 无缓存 — 全量拉取并保存到 SQLite
    df, source_name = _fetch_kline(code, level="daily", start_date=None, datalen=10000)

    # mootdx TCP fallback
    if df.empty:
        try:
            client = _get_mootdx_client()
            raw = client.bars(symbol=code, category=4, offset=800)
            if raw is not None and not raw.empty:
                raw = raw.drop(
                    columns=["datetime", "year", "month", "day", "hour", "minute"],
                    errors="ignore",
                )
                raw = raw.reset_index()
                raw = raw.rename(columns={
                    "datetime": "Date", "open": "Open", "close": "Close",
                    "high": "High", "low": "Low", "volume": "Volume",
                })
                df = raw[["Date", "Open", "High", "Low", "Close", "Volume"]]
                df["Date"] = pd.to_datetime(df["Date"])
                source_name = "mootdx"
        except Exception as e:
            raise ValueError(f"No OHLCV data available for {code}: {e}")

    if df.empty:
        raise ValueError(f"No OHLCV data available for {code}")

    # 保存到 SQLite
    _db_save(df, code, "daily", data_type="stock", source=source_name)

    # Filter by curr_date to prevent look-ahead bias
    cutoff = pd.to_datetime(curr_date)
    return df[df["Date"] <= cutoff]


# ===========================================================================
# 9 Vendor Methods (matching interface.py VENDOR_METHODS signatures)
# ===========================================================================


# ---- 1. get_stock_data ----


def get_stock_data(
    symbol: Annotated[str, "A-stock code (e.g. 688017, SH688017)"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """Get OHLCV stock price data via multi-source K-line fetcher."""
    code = _normalize_ticker(symbol)

    # Use multi-source fetcher with date filter
    df, data_source = _fetch_kline(
        code, level="daily", start_date=start_date, datalen=10000,
    )

    if df.empty:
        raise ValueError(
            f"No data found for A-stock '{code}' "
            f"between {start_date} and {end_date}. "
            f"All data sources (eastmoney/sina/tencent/tushare/mootdx) failed."
        )

    # Filter by end_date
    end_dt = pd.to_datetime(end_date)
    df = df[df["Date"] <= end_dt]

    for col in ["Open", "High", "Low", "Close"]:
        if col in df.columns:
            df[col] = df[col].round(2)

    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
    csv_out = df[["Date", "Open", "High", "Low", "Close", "Volume"]].to_csv(
        index=False
    )

    header = f"# Stock data for {code} (A-stock) from {start_date} to {end_date}\n"
    header += f"# Total records: {len(df)}\n"
    header += f"# Data source: {data_source}\n"
    header += (
        f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    )

    return header + csv_out


# ---- 2. get_indicators ----

# Supported technical indicators with descriptions
_INDICATOR_DESCRIPTIONS = {
    "close_50_sma": "50 SMA: Medium-term trend indicator.",
    "close_200_sma": "200 SMA: Long-term trend benchmark.",
    "close_10_ema": "10 EMA: Responsive short-term average.",
    "macd": "MACD: Momentum via EMA differences.",
    "macds": "MACD Signal: EMA smoothing of MACD line.",
    "macdh": "MACD Histogram: Gap between MACD and signal.",
    "rsi": "RSI: Momentum overbought/oversold indicator (70/30 thresholds).",
    "boll": "Bollinger Middle: 20 SMA basis for Bollinger Bands.",
    "boll_ub": "Bollinger Upper Band: 2 std devs above middle.",
    "boll_lb": "Bollinger Lower Band: 2 std devs below middle.",
    "atr": "ATR: Average True Range volatility measure.",
    "vwma": "VWMA: Volume-weighted moving average.",
    "mfi": "MFI: Money Flow Index (volume + price momentum).",
}


def get_indicators(
    symbol: Annotated[str, "A-stock code"],
    indicator: Annotated[
        str, "technical indicator (e.g. rsi, macd, close_50_sma)"
    ],
    curr_date: Annotated[str, "Current trading date, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    """Get technical indicators using stockstats on mootdx OHLCV data."""
    from stockstats import wrap

    code = _normalize_ticker(symbol)

    if indicator not in _INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator {indicator} not supported. "
            f"Choose from: {list(_INDICATOR_DESCRIPTIONS.keys())}"
        )

    try:
        data = _load_ohlcv_astock(code, curr_date)
        df = wrap(data)
        df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")

        # Trigger stockstats calculation
        df[indicator]

        # Build date -> value lookup
        ind_dict = {}
        for _, row in df.iterrows():
            d = row["Date"]
            v = row[indicator]
            ind_dict[d] = "N/A" if pd.isna(v) else str(round(float(v), 4))

        # Generate output for look_back window
        curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
        before = curr_dt - relativedelta(days=look_back_days)

        lines = []
        dt = curr_dt
        while dt >= before:
            ds = dt.strftime("%Y-%m-%d")
            val = ind_dict.get(ds, "N/A: Not a trading day (weekend or holiday)")
            lines.append(f"{ds}: {val}")
            dt -= relativedelta(days=1)

        result = (
            f"## {indicator} values for {code} "
            f"from {before.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
            + "\n".join(lines)
            + "\n\n"
            + _INDICATOR_DESCRIPTIONS.get(indicator, "")
        )
        return result

    except Exception as e:
        return f"Error calculating {indicator} for {code}: {str(e)}"


# ---- 3. get_fundamentals ----


def get_fundamentals(
    ticker: Annotated[str, "A-stock code"],
    curr_date: Annotated[str, "current date"] = None,
) -> str:
    """Get company fundamentals from Tencent + mootdx + Eastmoney + 同花顺."""
    code = _normalize_ticker(ticker)

    try:
        lines = []

        # --- Tencent: real-time valuation ---
        try:
            tq = _tencent_quote([code])
            if code in tq:
                q = tq[code]
                lines.extend(
                    [
                        f"Name: {q['name']}",
                        f"Price: {q['price']}",
                        f"PE (TTM): {q['pe_ttm']}",
                        f"PE (Static): {q['pe_static']}",
                        f"PB: {q['pb']}",
                        f"Market Cap (100M CNY): {q['mcap_yi']}",
                        f"Float Market Cap (100M CNY): {q['float_mcap_yi']}",
                        f"Turnover Rate: {q['turnover_pct']}%",
                        f"Change: {q['change_pct']}%",
                        f"Limit Up: {q['limit_up']}",
                        f"Limit Down: {q['limit_down']}",
                    ]
                )
        except Exception as e:
            logger.warning("Tencent quote failed for %s: %s", code, e)

        # --- mootdx: financial snapshot (quarterly) ---
        try:
            client = _get_mootdx_client()
            fin = client.finance(symbol=code)
            if fin is not None and not (
                isinstance(fin, pd.DataFrame) and fin.empty
            ):
                row = fin.iloc[0] if isinstance(fin, pd.DataFrame) else fin
                field_map = {
                    "eps": "EPS (Quarterly)",
                    "bvps": "Book Value Per Share",
                    "roe": "ROE (%)",
                    "profit": "Net Profit",
                    "income": "Revenue",
                    "liutongguben": "Float Shares",
                    "zongguben": "Total Shares",
                }
                idx = row.index if hasattr(row, "index") else []
                for field, label in field_map.items():
                    if field in idx:
                        val = row[field]
                        if val is not None and str(val) != "nan":
                            lines.append(f"{label}: {val}")
        except Exception as e:
            logger.warning("mootdx finance failed for %s: %s", code, e)

        # --- Eastmoney push2: basic stock info (direct HTTP) ---
        _push2_ok = False
        try:
            market_code = 1 if code.startswith("6") else 0
            _info_url = "http://push2.eastmoney.com/api/qt/stock/get"
            _info_params = {
                "fltt": "2",
                "invt": "2",
                "fields": "f57,f58,f84,f85,f127,f116,f117,f189,f43",
                "secid": f"{market_code}.{code}",
            }
            r = _requests.get(
                _info_url, params=_info_params,
                headers={"User-Agent": _UA}, timeout=10,
                proxies=_NO_PROXY
            )
            d = r.json().get("data", {})
            if d:
                _push2_ok = True
                if d.get("f127"):
                    lines.append(f"行业: {d['f127']}")
                if d.get("f84"):
                    lines.append(f"总股本: {d['f84']}")
                if d.get("f85"):
                    lines.append(f"流通股本: {d['f85']}")
                if d.get("f116"):
                    lines.append(f"总市值: {d['f116']}")
                if d.get("f117"):
                    lines.append(f"流通市值: {d['f117']}")
                if d.get("f189"):
                    lines.append(f"上市日期: {d['f189']}")
        except Exception as e:
            logger.warning("eastmoney push2 stock info failed for %s: %s", code, e)

        # --- 备选: 东财 datacenter 获取行业/市值/股本 (push2 被阻断时) ---
        if not _push2_ok:
            try:
                # 行业信息: 从东财 datacenter 个股信息获取
                secid = _eastmoney_secid(code)
                info_url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
                info_params = {
                    "reportName": "RPT_F10_BASIC_ORGINFO",
                    "columns": "SECURITY_CODE,EM_INDUSTRY,TOTAL_SHARES,LISTING_DATE",
                    "filter": f'(SECURITY_CODE="{code}")',
                    "pageNumber": 1,
                    "pageSize": 1,
                    "sortTypes": -1,
                    "sortColumns": "UPDATE_DATE",
                    "source": "HSF10",
                    "client": "PC",
                }
                r = _requests.get(info_url, params=info_params,
                                  headers={"User-Agent": _UA}, timeout=10,
                                  proxies=_NO_PROXY)
                d = r.json().get("result", {})
                if d and d.get("data"):
                    row = d["data"][0]
                    if row.get("EM_INDUSTRY"):
                        lines.append(f"行业: {row['EM_INDUSTRY']}")
                    if row.get("TOTAL_SHARES"):
                        lines.append(f"总股本: {row['TOTAL_SHARES']}")
                    if row.get("LISTING_DATE"):
                        lines.append(f"上市日期: {str(row['LISTING_DATE'])[:10]}")
                    # 从腾讯 quote 补充市值
                    try:
                        tq2 = _tencent_quote([code])
                        if code in tq2:
                            q = tq2[code]
                            if q.get("mcap_yi"):
                                lines.append(f"总市值: {q['mcap_yi']}亿")
                            if q.get("float_mcap_yi"):
                                lines.append(f"流通市值: {q['float_mcap_yi']}亿")
                    except Exception:
                        pass
            except Exception as e:
                logger.warning("datacenter stock info fallback failed for %s: %s", code, e)

        # --- 同花顺 direct HTTP: consensus EPS forecast ---
        try:
            forecast_df = _ths_eps_forecast(code)
            if forecast_df is not None and not forecast_df.empty:
                lines.append("\n--- Consensus EPS Forecast (同花顺) ---")
                eps_by_year = {}
                for _, row in forecast_df.iterrows():
                    year = str(row.iloc[0]) if len(row) > 0 else ""
                    mean_eps_val = row.iloc[3] if len(row) > 3 else 0
                    count_val = row.iloc[1] if len(row) > 1 else 0
                    min_eps_val = row.iloc[2] if len(row) > 2 else "N/A"
                    max_eps_val = row.iloc[4] if len(row) > 4 else "N/A"
                    try:
                        mean_eps = float(mean_eps_val)
                    except (ValueError, TypeError):
                        mean_eps = 0
                    try:
                        count = int(count_val)
                    except (ValueError, TypeError):
                        count = 0
                    lines.append(
                        f"FY{year}: EPS={mean_eps} "
                        f"(range {min_eps_val}~{max_eps_val}, {count} analysts)"
                    )
                    if count < 3:
                        lines.append("  Warning: low coverage (<3 analysts)")
                    eps_by_year[year] = mean_eps

                # Forward PE / PEG / PE digestion
                try:
                    tq = _tencent_quote([code])
                    if code in tq:
                        price = tq[code]["price"]
                        years_sorted = sorted(eps_by_year.keys())
                        if years_sorted and eps_by_year.get(years_sorted[0], 0) > 0:
                            eps_cur = eps_by_year[years_sorted[0]]
                            fwd_pe = price / eps_cur
                            lines.append(
                                f"\nForward PE (FY{years_sorted[0]}): "
                                f"{fwd_pe:.1f}x (price={price}, EPS={eps_cur})"
                            )
                            if (
                                len(years_sorted) >= 2
                                and eps_by_year.get(years_sorted[1], 0) > 0
                            ):
                                eps_next = eps_by_year[years_sorted[1]]
                                cagr = eps_next / eps_cur - 1
                                if cagr > 0:
                                    peg = fwd_pe / (cagr * 100)
                                    lines.append(
                                        f"PEG: {peg:.2f} "
                                        f"(EPS CAGR={cagr * 100:.0f}%)"
                                    )
                                    if fwd_pe > 30:
                                        digest = math.log(fwd_pe / 30) / math.log(
                                            1 + cagr
                                        )
                                        lines.append(
                                            f"PE Digestion to 30x: {digest:.1f} years"
                                        )
                                    else:
                                        lines.append("PE already below 30x target")
                                else:
                                    lines.append(
                                        f"EPS declining ({cagr * 100:.0f}%), "
                                        f"PEG not applicable"
                                    )
                except Exception as e:
                    logger.warning("Forward PE calc failed for %s: %s", code, e)
        except Exception as e:
            logger.warning("Consensus EPS forecast failed for %s: %s", code, e)

        if not lines:
            raise ValueError(f"No fundamentals data found for A-stock '{code}'")

        header = f"# Company Fundamentals for {code} (A-stock)\n"
        header += (
            f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        )

        return header + "\n".join(lines)

    except Exception as e:
        return f"Error retrieving fundamentals for {code}: {str(e)}"


# ---- 4. get_balance_sheet ----


def _sina_stock_code(code: str) -> str:
    """Pure 6-digit code → sina format (sh688017 / sz000001 / bj832000)."""
    return f"{_get_prefix(code)}{code}"


def _get_financial_report_sina(
    code: str, report_type: str, freq: str, curr_date: str = None,
) -> pd.DataFrame:
    """Shared helper: fetch financial report via Sina direct HTTP API.

    report_type: '资产负债表' | '利润表' | '现金流量表'

    Sina 2025 API returns:
      result.data.report_list: {"20250930": {"data": [{item_field, item_title, item_value, ...}], ...}, ...}
      result.data.report_date: [{"date_value": "20250930", "date_description": "2025三季报", "date_type": 3}, ...]
    """
    _report_type_map = {
        "资产负债表": "fzb",
        "利润表": "lrb",
        "现金流量表": "llb",
    }
    source_type = _report_type_map.get(report_type, "lrb")

    prefix = "sh" if code.startswith("6") else "sz"
    paper_code = f"{prefix}{code}"
    url = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
    params = {
        "paperCode": paper_code,
        "source": source_type,
        "type": "0",
        "page": "1",
        "num": "20",
    }
    r = _requests.get(url, params=params, headers={"User-Agent": _UA}, timeout=15,
                      proxies=_NO_PROXY)
    d = r.json()

    result_data = d.get("result", {}).get("data", {})

    # New API format: report_list is a dict keyed by report date
    report_list = result_data.get("report_list", {})
    if not isinstance(report_list, dict) or not report_list:
        # Old API fallback (unlikely but safe)
        items = result_data.get(source_type, [])
        if isinstance(items, list) and items:
            df = pd.DataFrame(items)
            if curr_date and "报告日" in df.columns:
                df["报告日"] = pd.to_datetime(df["报告日"], errors="coerce")
                cutoff = pd.to_datetime(curr_date)
                df = df[df["报告日"] <= cutoff]
            return df.head(8)
        return pd.DataFrame()

    # Parse new format: build a flat DataFrame from report_list
    rows = []
    for date_key, report in report_list.items():
        if not isinstance(report, dict):
            continue
        items = report.get("data", [])
        if not isinstance(items, list):
            continue

        # Filter by freq: 1231 = annual report
        is_annual = date_key.endswith("1231")
        if freq.lower() == "annual" and not is_annual:
            continue

        # Filter by curr_date
        if curr_date:
            try:
                report_dt = pd.to_datetime(date_key)
                cutoff = pd.to_datetime(curr_date)
                if report_dt > cutoff:
                    continue
            except Exception:
                pass

        # Build row: report date + key items as columns
        row = {"报告日": date_key}
        for item in items:
            title = item.get("item_title", "")
            value = item.get("item_value")
            row[title] = value
        rows.append(row)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    # Convert 报告日 to proper date format
    try:
        df["报告日"] = pd.to_datetime(df["报告日"], format="%Y%m%d")
    except Exception:
        pass

    return df.head(8)


def _tushare_financial_report(
    code: str, report_type: str, freq: str, curr_date: str = None,
) -> pd.DataFrame:
    """Fetch financial report via Tushare Pro API (primary source).

    report_type: 'income' | 'balance' | 'cashflow'
    Returns DataFrame or empty.
    """
    token = os.environ.get("SYSTEM_TUSHARE_TOKEN", "")
    if not token:
        return pd.DataFrame()

    try:
        import tushare as ts
        ts.set_token(token)
        pro = ts.pro_api()
    except (ImportError, Exception) as e:
        logger.warning("Tushare init failed: %s", e)
        return pd.DataFrame()

    # Convert 6-digit code to ts_code format
    if code.startswith(("6", "9")):
        ts_code = f"{code}.SH"
    elif code.startswith("8") or code.startswith("4"):
        ts_code = f"{code}.BJ"
    else:
        ts_code = f"{code}.SZ"

    # Build kwargs
    kwargs = {"ts_code": ts_code}
    if curr_date:
        try:
            end_date_fmt = curr_date.replace("-", "")
            kwargs["end_date"] = end_date_fmt
        except Exception:
            pass

    try:
        if report_type == "income":
            df = pro.income(**kwargs)
        elif report_type == "balance":
            df = pro.balancesheet(**kwargs)
        elif report_type == "cashflow":
            df = pro.cashflow(**kwargs)
        else:
            return pd.DataFrame()
    except Exception as e:
        logger.warning("Tushare %s failed for %s: %s", report_type, code, e)
        return pd.DataFrame()

    if df is None or df.empty:
        return pd.DataFrame()

    # Filter by freq: 'annual' means only keep rows where end_date ends with '1231'
    if freq.lower() == "annual" and "end_date" in df.columns:
        df = df[df["end_date"].astype(str).str.endswith("1231")]

    # Filter by curr_date: only keep reports announced on or before curr_date
    if curr_date and "ann_date" in df.columns:
        try:
            cutoff = curr_date.replace("-", "")
            df = df[df["ann_date"].astype(str) <= cutoff]
        except Exception:
            pass

    df = df.head(8)
    return df


def _eastmoney_financial_report(
    code: str, report_type: str, freq: str, curr_date: str = None,
) -> pd.DataFrame:
    """Fetch financial report via 东财 datacenter (backup for Tushare/Sina).

    report_type: 'income' | 'balance' | 'cashflow'
    Returns DataFrame or empty.
    """
    _RPT_MAP = {
        "income": "RPT_DMSK_FN_INCOME",
        "balance": "RPT_DMSK_FN_BALANCE",
        "cashflow": "RPT_DMSK_FN_CASHFLOW",
    }
    rpt_name = _RPT_MAP.get(report_type)
    if not rpt_name:
        return pd.DataFrame()

    filter_str = f'(SECURITY_CODE="{code}")'
    data = _eastmoney_datacenter(
        rpt_name, filter_str=filter_str,
        page_size=8, sort_columns="REPORT_DATE", sort_types="-1",
    )
    if not data:
        return pd.DataFrame()

    rows = []
    for row in data:
        report_date = str(row.get("REPORT_DATE", ""))[:10]
        if not report_date:
            continue
        # freq filter: annual = only 1231 reports
        if freq.lower() == "annual" and not report_date.endswith("12-31"):
            continue
        # curr_date filter
        if curr_date:
            try:
                if report_date > curr_date:
                    continue
            except Exception:
                pass
        rows.append(row)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    if "REPORT_DATE" in df.columns:
        df["REPORT_DATE"] = df["REPORT_DATE"].astype(str).str[:10]
    return df


def get_balance_sheet(
    ticker: Annotated[str, "A-stock code"],
    freq: Annotated[str, "frequency: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    """Get balance sheet via Tushare Pro API, with Sina and Eastmoney fallback."""
    code = _normalize_ticker(ticker)

    # Primary: Tushare Pro API
    try:
        df = _tushare_financial_report(code, "balance", freq, curr_date)
        if not df.empty:
            csv_string = df.to_csv(index=False)
            header = f"# Balance Sheet for {code} (A-stock, {freq})\n"
            header += "# Data source: Tushare Pro API\n"
            header += (
                f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            )
            return header + csv_string
    except Exception as e:
        logger.warning("Tushare balance sheet failed for %s: %s", code, e)

    # Fallback 1: Sina direct HTTP
    try:
        df = _get_financial_report_sina(code, "资产负债表", freq, curr_date)
        if not df.empty:
            csv_string = df.to_csv(index=False)
            header = f"# Balance Sheet for {code} (A-stock, {freq})\n"
            header += "# Data source: sina direct HTTP\n"
            header += (
                f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            )
            return header + csv_string
    except Exception as e:
        logger.warning("Sina balance sheet failed for %s: %s", code, e)

    # Fallback 2: Eastmoney datacenter
    try:
        df = _eastmoney_financial_report(code, "balance", freq, curr_date)
        if not df.empty:
            csv_string = df.to_csv(index=False)
            header = f"# Balance Sheet for {code} (A-stock, {freq})\n"
            header += "# Data source: eastmoney datacenter\n"
            header += (
                f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            )
            return header + csv_string
    except Exception as e:
        logger.warning("Eastmoney balance sheet failed for %s: %s", code, e)

    return f"Error retrieving balance sheet for {code}: all sources failed"


# ---- 5. get_cashflow ----


def get_cashflow(
    ticker: Annotated[str, "A-stock code"],
    freq: Annotated[str, "frequency: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    """Get cash flow statement via Tushare Pro API, with Sina and Eastmoney fallback."""
    code = _normalize_ticker(ticker)

    # Primary: Tushare Pro API
    try:
        df = _tushare_financial_report(code, "cashflow", freq, curr_date)
        if not df.empty:
            csv_string = df.to_csv(index=False)
            header = f"# Cash Flow for {code} (A-stock, {freq})\n"
            header += "# Data source: Tushare Pro API\n"
            header += (
                f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            )
            return header + csv_string
    except Exception as e:
        logger.warning("Tushare cashflow failed for %s: %s", code, e)

    # Fallback 1: Sina direct HTTP
    try:
        df = _get_financial_report_sina(code, "现金流量表", freq, curr_date)
        if not df.empty:
            csv_string = df.to_csv(index=False)
            header = f"# Cash Flow for {code} (A-stock, {freq})\n"
            header += "# Data source: sina direct HTTP\n"
            header += (
                f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            )
            return header + csv_string
    except Exception as e:
        logger.warning("Sina cashflow failed for %s: %s", code, e)

    # Fallback 2: Eastmoney datacenter
    try:
        df = _eastmoney_financial_report(code, "cashflow", freq, curr_date)
        if not df.empty:
            csv_string = df.to_csv(index=False)
            header = f"# Cash Flow for {code} (A-stock, {freq})\n"
            header += "# Data source: eastmoney datacenter\n"
            header += (
                f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            )
            return header + csv_string
    except Exception as e:
        logger.warning("Eastmoney cashflow failed for %s: %s", code, e)

    return f"Error retrieving cash flow for {code}: all sources failed"


# ---- 6. get_income_statement ----


def get_income_statement(
    ticker: Annotated[str, "A-stock code"],
    freq: Annotated[str, "frequency: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    """Get income statement via Tushare Pro API, with Sina and Eastmoney fallback."""
    code = _normalize_ticker(ticker)

    # Primary: Tushare Pro API
    try:
        df = _tushare_financial_report(code, "income", freq, curr_date)
        if not df.empty:
            csv_string = df.to_csv(index=False)
            header = f"# Income Statement for {code} (A-stock, {freq})\n"
            header += "# Data source: Tushare Pro API\n"
            header += (
                f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            )
            return header + csv_string
    except Exception as e:
        logger.warning("Tushare income statement failed for %s: %s", code, e)

    # Fallback 1: Sina direct HTTP
    try:
        df = _get_financial_report_sina(code, "利润表", freq, curr_date)
        if not df.empty:
            csv_string = df.to_csv(index=False)
            header = f"# Income Statement for {code} (A-stock, {freq})\n"
            header += "# Data source: sina direct HTTP\n"
            header += (
                f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            )
            return header + csv_string
    except Exception as e:
        logger.warning("Sina income statement failed for %s: %s", code, e)

    # Fallback 2: Eastmoney datacenter
    try:
        df = _eastmoney_financial_report(code, "income", freq, curr_date)
        if not df.empty:
            csv_string = df.to_csv(index=False)
            header = f"# Income Statement for {code} (A-stock, {freq})\n"
            header += "# Data source: eastmoney datacenter\n"
            header += (
                f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            )
            return header + csv_string
    except Exception as e:
        logger.warning("Eastmoney income statement failed for %s: %s", code, e)

    return f"Error retrieving income statement for {code}: all sources failed"


# ---- 7. get_news ----


def _fetch_news_eastmoney(code: str, page_size: int = 20) -> list[dict]:
    """Direct East Money search API for individual stock news."""
    url = "https://search-api-web.eastmoney.com/search/jsonp"
    inner_param = {
        "uid": "",
        "keyword": code,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "default",
                "sort": "default",
                "pageIndex": 1,
                "pageSize": page_size,
                "preTag": "",
                "postTag": "",
            }
        },
    }
    params = {
        "cb": "jQuery",
        "param": _json.dumps(inner_param, ensure_ascii=False),
        "_": "1",
    }
    headers = {
        "Referer": "https://so.eastmoney.com/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
    }

    resp = _requests.get(url, params=params, headers=headers, timeout=15,
                         proxies=_NO_PROXY)
    resp.raise_for_status()
    text = resp.text
    text = text[text.index("(") + 1 : text.rindex(")")]
    data = _json.loads(text)

    articles: list[dict] = []
    for item in data.get("result", {}).get("cmsArticleWebOld", []):
        articles.append({
            "title": item.get("title", ""),
            "content": item.get("content", ""),
            "time": item.get("date", ""),
            "source": item.get("mediaName", "东方财富"),
            "url": item.get("url", ""),
        })
    return articles


def _fetch_news_sina(code: str, page_size: int = 20) -> list[dict]:
    """Sina Finance stock news API (backup source)."""
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    url = (
        f"https://vip.stock.finance.sina.com.cn/corp/view/"
        f"vCB_AllNewsStock.php?symbol={prefix}{code}&Page=1"
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
        "Referer": "https://finance.sina.com.cn/",
    }

    resp = _requests.get(url, headers=headers, timeout=15,
                         proxies=_NO_PROXY)
    resp.raise_for_status()
    resp.encoding = "gb2312"
    html = resp.text

    articles: list[dict] = []
    rows = _re.findall(
        r"(\d{4}-\d{2}-\d{2})\s*(?:&nbsp;)*(\d{2}:\d{2})\s*(?:&nbsp;)*"
        r"<a[^>]+href='([^']+)'[^>]*>([^<]+)</a>",
        html,
    )
    for date_str, time_str, link, title in rows[:page_size]:
        articles.append({
            "title": title.strip(),
            "content": "",
            "time": f"{date_str} {time_str}",
            "source": "新浪财经",
            "url": link,
        })
    return articles


def get_news(
    ticker: Annotated[str, "A-stock code"],
    start_date: Annotated[str, "Start date yyyy-mm-dd"],
    end_date: Annotated[str, "End date yyyy-mm-dd"],
) -> str:
    """Get stock-specific news via East Money direct API (Sina as fallback)."""
    code = _normalize_ticker(ticker)

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    articles: list[dict] = []
    source_label = ""

    try:
        articles = _fetch_news_eastmoney(code)
        source_label = "东方财富"
    except Exception as e:
        logger.warning("East Money news fetch failed for %s: %s", code, e)

    if not articles:
        try:
            articles = _fetch_news_sina(code)
            source_label = "新浪财经"
        except Exception as e:
            logger.warning("Sina news fetch failed for %s: %s", code, e)

    if not articles:
        raise ValueError(f"No news found for A-stock '{code}'")

    # Filter by date range, but keep all if date filter removes everything
    filtered_articles = []
    for art in articles:
        pub_time = art.get("time", "")
        try:
            pub_dt = datetime.strptime(pub_time[:10], "%Y-%m-%d")
            if start_dt <= pub_dt <= end_dt:
                filtered_articles.append(art)
        except (ValueError, IndexError):
            # 日期解析失败的新闻也保留
            filtered_articles.append(art)

    # 如果日期过滤后为空，使用全部新闻并提醒用户
    if not filtered_articles and articles:
        filtered_articles = articles
        date_warn = (
            f"⚠️ 请求日期范围 {start_date}~{end_date} 内无新闻，"
            f"已返回全部可用新闻（共{len(articles)}条）。\n\n"
        )
    else:
        date_warn = ""

    if not filtered_articles:
        raise ValueError(f"No news found for A-stock '{code}'")

    news_str = ""
    count = 0
    for art in filtered_articles:
        title = art["title"]
        content = art.get("content", "")
        source = art.get("source", source_label)
        link = art.get("url", "")

        news_str += f"### {title} (source: {source})\n"
        if content:
            snippet = content[:300] + "..." if len(content) > 300 else content
            news_str += f"{snippet}\n"
        if link and link != "nan":
            news_str += f"Link: {link}\n"
        news_str += "\n"
        count += 1

    if count == 0:
        return (
            f"No news found for A-stock '{code}' "
            f"between {start_date} and {end_date}"
        )

    return (
        f"## {code} (A-stock) News, from {start_date} to {end_date}:\n\n"
        + date_warn
        + news_str
    )


# ---- 8. get_global_news ----


def get_global_news(
    curr_date: Annotated[str, "Current date yyyy-mm-dd"],
    look_back_days: Annotated[int, "Days to look back"] = 7,
    limit: Annotated[int, "Max articles"] = 10,
) -> str:
    """Get China/global financial news via direct HTTP (CLS + Eastmoney)."""
    start_dt = datetime.strptime(curr_date, "%Y-%m-%d") - relativedelta(
        days=look_back_days
    )
    start_date = start_dt.strftime("%Y-%m-%d")

    all_news: list[dict] = []

    # Source 1: CLS wire (财联社快讯) — direct HTTP
    try:
        cls_url = "https://www.cls.cn/nodeapi/telegraphList"
        cls_params = {"rn": str(limit), "page": "1"}
        cls_headers = {"User-Agent": _UA, "Referer": "https://www.cls.cn/"}
        r_cls = _requests.get(cls_url, params=cls_params, headers=cls_headers, timeout=10,
                              proxies=_NO_PROXY)
        d_cls = r_cls.json()
        for item in d_cls.get("data", {}).get("roll_data", []):
            title = item.get("title", "") or item.get("brief", "")
            content = item.get("content", "") or item.get("brief", "")
            ctime = item.get("ctime", "")
            # ctime is unix timestamp
            pub_time = ""
            if ctime:
                try:
                    pub_time = datetime.fromtimestamp(int(ctime)).strftime("%Y-%m-%d %H:%M")
                except (ValueError, TypeError, OSError):
                    pub_time = str(ctime)
            all_news.append({
                "title": title,
                "content": content,
                "time": pub_time,
                "source": "CLS Wire",
            })
    except Exception as e:
        logger.warning("CLS news fetch failed: %s", e)

    # Source 2: Eastmoney global (东财7x24资讯) — direct HTTP
    try:
        em_url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
        em_params = {
            "client": "web",
            "biz": "web_724",
            "fastColumn": "102",
            "sortEnd": "",
            "pageSize": str(limit),
            "req_trace": str(uuid.uuid4()),
        }
        em_headers = {"User-Agent": _UA, "Referer": "https://kuaixun.eastmoney.com/"}
        r_em = _requests.get(em_url, params=em_params, headers=em_headers, timeout=10,
                             proxies=_NO_PROXY)
        d_em = r_em.json()
        for item in d_em.get("data", {}).get("fastNewsList", []):
            title = item.get("title", "")
            summary = item.get("summary", "")[:200]
            pub_time = item.get("showTime", "")
            all_news.append({
                "title": title,
                "content": summary,
                "time": pub_time,
                "source": "Eastmoney Global",
            })
    except Exception as e:
        logger.warning("Eastmoney global news fetch failed: %s", e)

    if not all_news:
        raise ValueError(f"No global news found for {curr_date}")

    # Deduplicate by title
    seen: set[str] = set()
    unique: list[dict] = []
    for n in all_news:
        if n["title"] not in seen:
            seen.add(n["title"])
            unique.append(n)

    news_str = ""
    for n in unique[:limit]:
        news_str += f"### {n['title']} (source: {n['source']})\n"
        if n.get("content"):
            snippet = (
                n["content"][:300] + "..."
                if len(n["content"]) > 300
                else n["content"]
            )
            news_str += f"{snippet}\n"
        news_str += "\n"

    return (
        f"## China & Global Market News, from {start_date} to {curr_date}:\n\n"
        + news_str
    )


def get_insider_transactions(
    ticker: Annotated[str, "A-stock code"],
) -> str:
    """Get shareholder/insider activity via mootdx F10 (primary) or Eastmoney datacenter (fallback).

    Note: A-stock insider transaction data differs from US markets.
    Primary: mootdx F10 shareholder research; Fallback: 东财 十大股东/十大流通股东.
    """
    code = _normalize_ticker(ticker)
    lines = [
        f"# Shareholder Research for {code} (A-stock)",
        f"# Note: A-stock equivalent of insider transactions",
        f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    got_data = False

    # Source 1: mootdx F10 (primary)
    try:
        client = _get_mootdx_client()
        text = client.F10(symbol=code, name="股东研究")

        if text and text.strip():
            import re

            sec4_hits = list(re.finditer(r"\r?\n【4\.股东变化】\r?\n", text))
            if sec4_hits:
                sec4_pos = sec4_hits[-1].start()
                before_sec4 = text[:sec4_pos]
                sec4_text = text[sec4_pos:]
                cut_at = 2000
                if len(sec4_text) > cut_at:
                    sec4_text = (
                        sec4_text[:cut_at]
                        + "\n\n(... older shareholder history omitted, "
                        + f"{len(text) - sec4_pos - cut_at} chars truncated ...)"
                    )
                text = before_sec4 + sec4_text

            lines.append(text)
            lines.append("\n# Source: mootdx F10")
            got_data = True
    except Exception as e:
        logger.warning("mootdx F10 failed for %s: %s", code, e)

    # Source 2: 东财 datacenter 十大股东 + 十大流通股东 (fallback)
    if not got_data:
        try:
            # 十大股东
            holders = _eastmoney_datacenter(
                "RPT_F10_EH_HOLDERS",
                filter_str=f'(SECURITY_CODE="{code}")',
                page_size=10, sort_columns="END_DATE", sort_types="-1",
            )
            if holders:
                # Group by END_DATE
                by_date: dict[str, list] = {}
                for h in holders:
                    ed = str(h.get("END_DATE", ""))[:10]
                    by_date.setdefault(ed, []).append(h)

                for ed in sorted(by_date.keys(), reverse=True)[:2]:
                    lines.append(f"## 十大股东 ({ed})")
                    lines.append("排名 | 股东名称 | 持股数 | 持股比例 | 变动")
                    for h in by_date[ed]:
                        rank = h.get("HOLDER_RANK", "")
                        name = h.get("HOLDER_NAME", "")
                        hold = h.get("HOLD_NUM", "")
                        ratio = h.get("HOLD_NUM_RATIO", "")
                        change = h.get("CHANGE_RATIO", "")
                        lines.append(f"  {rank} | {name} | {hold} | {ratio}% | {change}%")
                    lines.append("")

            # 十大流通股东
            free_holders = _eastmoney_datacenter(
                "RPT_F10_EH_FREEHOLDERS",
                filter_str=f'(SECURITY_CODE="{code}")',
                page_size=10, sort_columns="END_DATE", sort_types="-1",
            )
            if free_holders:
                by_date2: dict[str, list] = {}
                for h in free_holders:
                    ed = str(h.get("END_DATE", ""))[:10]
                    by_date2.setdefault(ed, []).append(h)

                for ed in sorted(by_date2.keys(), reverse=True)[:2]:
                    lines.append(f"## 十大流通股东 ({ed})")
                    lines.append("排名 | 股东名称 | 持股数 | 持股比例 | 变动")
                    for h in by_date2[ed]:
                        rank = h.get("HOLDER_RANK", "")
                        name = h.get("HOLDER_NAME", "")
                        hold = h.get("HOLD_NUM", "")
                        ratio = h.get("FREE_HOLDNUM_RATIO", "")
                        change = h.get("CHANGE_RATIO", "")
                        lines.append(f"  {rank} | {name} | {hold} | {ratio}% | {change}%")
                    lines.append("")

            if holders or free_holders:
                lines.append("# Source: 东财 datacenter (mootdx F10 不可用时的备选)")
                got_data = True
        except Exception as e:
            logger.warning("东财股东数据查询失败 for %s: %s", code, e)

    if not got_data:
        raise ValueError(f"No insider/shareholder data found for A-stock '{code}'")

    return "\n".join(lines)

# ---- 10. get_profit_forecast ----


def get_profit_forecast(
    ticker: Annotated[str, "A-stock code"],
    curr_date: Annotated[str, "current date (unused, for interface compat)"] = None,
) -> str:
    """Get consensus EPS forecasts with forward valuation.

    Primary: 同花顺 analyst consensus; Fallback: 东财 datacenter 利润表推算 EPS.
    """
    code = _normalize_ticker(ticker)

    lines = [
        f"# Profit Forecast for {code} (A-stock)",
        f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    got_data = False

    # Source 1: 同花顺 analyst consensus (primary)
    try:
        df = _ths_eps_forecast(code)

        if df is not None and not df.empty:
            lines.append("## Consensus EPS Forecast (同花顺)")
            eps_by_year = {}
            for _, row in df.iterrows():
                year = str(row.iloc[0]) if len(row) > 0 else ""
                count_val = row.iloc[1] if len(row) > 1 else 0
                mean_eps_val = row.iloc[3] if len(row) > 3 else 0
                min_eps_val = row.iloc[2] if len(row) > 2 else "N/A"
                max_eps_val = row.iloc[4] if len(row) > 4 else "N/A"
                try:
                    count = int(count_val)
                except (ValueError, TypeError):
                    count = 0
                try:
                    mean_eps = float(mean_eps_val)
                except (ValueError, TypeError):
                    mean_eps = 0
                lines.append(
                    f"FY{year}: EPS={mean_eps} (range {min_eps_val}~{max_eps_val}), "
                    f"analysts={count}"
                )
                if count < 3:
                    lines.append("  Warning: low coverage (<3 analysts)")
                eps_by_year[year] = mean_eps

            # Forward valuation from Tencent
            try:
                tq = _tencent_quote([code])
                if code in tq:
                    price = tq[code]["price"]
                    pe_ttm = tq[code]["pe_ttm"]
                    lines.append(f"\nCurrent: price={price}, PE(TTM)={pe_ttm}")

                    years_sorted = sorted(eps_by_year.keys())
                    if years_sorted and eps_by_year.get(years_sorted[0], 0) > 0:
                        eps_cur = eps_by_year[years_sorted[0]]
                        fwd_pe = price / eps_cur
                        lines.append(f"Forward PE (FY{years_sorted[0]}): {fwd_pe:.1f}x")
                        if (
                            len(years_sorted) >= 2
                            and eps_by_year.get(years_sorted[1], 0) > 0
                        ):
                            eps_next = eps_by_year[years_sorted[1]]
                            cagr = eps_next / eps_cur - 1
                            if cagr > 0:
                                peg = fwd_pe / (cagr * 100)
                                lines.append(f"PEG: {peg:.2f} (CAGR={cagr * 100:.0f}%)")
                                if fwd_pe > 30:
                                    digest = math.log(fwd_pe / 30) / math.log(1 + cagr)
                                    lines.append(f"PE Digestion to 30x: {digest:.1f} years")
                            else:
                                lines.append(
                                    f"EPS declining ({cagr * 100:.0f}%), PEG not applicable"
                                )
            except Exception as e:
                logger.warning("Forward PE calc failed for %s: %s", code, e)

            lines.append("\n# Source: 同花顺 analyst consensus")
            got_data = True
    except Exception as e:
        logger.warning("同花顺 EPS forecast failed for %s: %s", code, e)

    # Source 2: 东财 datacenter 利润表推算 EPS (fallback)
    if not got_data:
        try:
            data = _eastmoney_datacenter(
                "RPT_DMSK_FN_INCOME",
                filter_str=f'(SECURITY_CODE="{code}")',
                page_size=8, sort_columns="REPORT_DATE", sort_types="-1",
            )
            if data:
                lines.append("## EPS from Financial Reports (东财)")
                eps_by_year = {}
                # Try to get total shares for EPS calculation
                total_shares = 0
                try:
                    tq = _tencent_quote([code])
                    if code in tq:
                        mcap = tq[code].get("mcap_yi", 0) * 1e8
                        price = tq[code].get("price", 0)
                        if price > 0:
                            total_shares = mcap / price
                except Exception:
                    pass

                for row in data:
                    report_date = str(row.get("REPORT_DATE", ""))[:10]
                    net_profit = row.get("PARENT_NETPROFIT")
                    revenue = row.get("TOTAL_OPERATE_INCOME")
                    net_ratio = row.get("PARENT_NETPROFIT_RATIO")
                    if net_profit is None:
                        continue
                    # Filter by freq
                    if curr_date and report_date > curr_date:
                        continue
                    profit_yi = float(net_profit) / 1e8
                    revenue_yi = float(revenue) / 1e8 if revenue else 0
                    eps_est = float(net_profit) / total_shares if total_shares > 0 else 0
                    lines.append(
                        f"{report_date}: 净利润={profit_yi:.2f}亿 "
                        f"营收={revenue_yi:.2f}亿 YoY={net_ratio or 'N/A'}% "
                        f"EPS≈{eps_est:.2f}"
                    )
                    eps_by_year[report_date[:4]] = eps_est

                lines.append("\n# Source: 东财 datacenter (同花顺不可用时的备选，EPS为估算)")
                got_data = True
        except Exception as e:
            logger.warning("东财利润表查询失败 for %s: %s", code, e)

    if not got_data:
        raise ValueError(f"No profit forecast data found for A-stock '{code}'")

    return "\n".join(lines)

# ---- 11. get_hot_stocks ----


def get_hot_stocks(
    curr_date: Annotated[str, "Date YYYY-MM-DD, empty string for today"] = "",
) -> str:
    """Get strong/limit-up stocks with topic attribution.

    Primary: 同花顺 editorial (human-curated reason tags);
    Fallback: 东财 push2 clist (涨幅排名).
    """
    if not curr_date or curr_date.strip() == "":
        curr_date = datetime.now().strftime("%Y-%m-%d")

    lines = [
        f"# Hot Stocks ({curr_date})",
        f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    got_data = False

    # Source 1: 同花顺 editorial (primary — human-curated reason tags)
    try:
        url = (
            f"http://zx.10jqka.com.cn/event/api/getharden/"
            f"date/{curr_date}/orderby/date/orderway/desc/charset/GBK/"
        )
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "Chrome/117.0.0.0 Safari/537.36"
            )
        }
        r = _requests.get(url, headers=headers, timeout=10, proxies=_NO_PROXY)
        data = r.json()

        if data.get("errocode", 0) != 0:
            raise ValueError(f"同花顺 API error: {data.get('errormsg', 'unknown')}")

        rows = data.get("data") or []
        if rows:
            lines.append(f"## 涨停板 ({len(rows)} stocks, 同花顺)")
            lines.append("代码 名称 | 涨幅 | 换手 | 原因")

            from collections import Counter

            all_tags: list[str] = []
            for row in rows:
                stk_code = row.get("code", "")
                name = row.get("name", "")
                reason = row.get("reason", "")
                zhangfu = row.get("zhangfu", "")
                huanshou = row.get("huanshou", "")
                chengjiaoe = row.get("chengjiaoe", "")
                dde = row.get("ddejingliang", "")

                lines.append(
                    f"  {stk_code} {name}: +{zhangfu}% "
                    f"换手{huanshou}% 成交额{chengjiaoe} "
                    f"大单净量{dde} | {reason}"
                )

                if reason:
                    tags = [t.strip() for t in str(reason).split("+") if t.strip()]
                    all_tags.extend(tags)

            if all_tags:
                cnt = Counter(all_tags)
                lines.append(f"\n## Theme Frequency (top 15)")
                for tag, n in cnt.most_common(15):
                    lines.append(f"  {tag}: {n} stocks")

            lines.append("\n# Source: 同花顺 editorial")
            got_data = True
    except Exception as e:
        logger.warning("同花顺涨停板 failed: %s", e)

    # Source 2: 东财 push2 clist — 涨幅排名 (fallback)
    if not got_data:
        try:
            url = "http://push2.eastmoney.com/api/qt/clist/get"
            params = {
                "pn": "1", "pz": "30", "po": "1", "np": "1",
                "fltt": "2", "invt": "2",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
                "fields": "f2,f3,f4,f5,f6,f7,f8,f12,f14,f15",
            }
            r = _requests.get(url, params=params, headers={"User-Agent": _UA},
                              timeout=10, proxies=_NO_PROXY)
            d = r.json()
            items = d.get("data", {}).get("diff", [])

            if items:
                limit_up_stocks = [it for it in items
                                   if isinstance(it.get("f3"), (int, float)) and it["f3"] >= 9.5]
                display = limit_up_stocks[:30] if limit_up_stocks else items[:20]
                lines.append(
                    f"## 涨幅排名 ({len(limit_up_stocks)} 涨停, 东财)"
                )
                lines.append("代码 名称 | 涨幅 | 换手 | 成交额(亿)")
                for it in display:
                    code_em = it.get("f12", "")
                    name_em = it.get("f14", "")
                    pct = it.get("f3", "")
                    turnover = it.get("f8", "")
                    amount = it.get("f6", 0)
                    amount_yi = float(amount) / 1e8 if amount else 0
                    lines.append(
                        f"  {code_em} {name_em}: +{pct}% "
                        f"换手{turnover}% 成交额{amount_yi:.1f}亿"
                    )
                lines.append("\n# Source: 东财 push2 (同花顺不可用时的备选)")
                got_data = True
        except Exception as e:
            logger.warning("东财涨幅排名 failed: %s", e)

    if not got_data:
        return (
            f"No hot stocks data for {curr_date} "
            f"(may be non-trading day or all sources failed)"
        )

    return "\n".join(lines)

# ---- 12. get_northbound_flow ----


def _northbound_cache_path() -> str:
    """Path to local CSV cache for northbound daily close snapshots."""
    return os.path.join(_cache_dir(), "northbound_daily.csv")


def _save_northbound_snapshot(date_str: str, hgt: float, sgt: float) -> None:
    """Append today's northbound close to local CSV cache (dedup by date)."""
    import csv

    path = _northbound_cache_path()
    existing: dict[str, tuple[str, str]] = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) >= 3:
                    existing[row[0]] = (row[1], row[2])
    existing[date_str] = (f"{hgt:.2f}", f"{sgt:.2f}")
    sorted_dates = sorted(existing.keys())
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "hgt", "sgt"])
        for d in sorted_dates:
            writer.writerow([d, existing[d][0], existing[d][1]])


def _load_northbound_history(n: int = 20) -> list[tuple[str, float, float]]:
    """Load last N days of northbound close data from local cache."""
    import csv

    path = _northbound_cache_path()
    if not os.path.exists(path):
        return []
    rows: list[tuple[str, float, float]] = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) >= 3:
                try:
                    rows.append((row[0], float(row[1]), float(row[2])))
                except ValueError:
                    continue
    return rows[-n:]


def get_northbound_flow(
    curr_date: Annotated[str, "Date YYYY-MM-DD"],
    include_history: Annotated[
        bool, "Include historical daily data (last 20 trading days)"
    ] = False,
) -> str:
    """Get northbound capital flow (沪深股通).

    Primary: 同花顺 hsgtApi; Fallback: 东财 push2 kamt.
    Realtime: minute-level cumulative net buying for HGT(沪股通) + SGT(深股通).
    History: self-cached daily close snapshots.
    """
    lines = [
        f"# Northbound Capital Flow ({curr_date})",
        "",
    ]

    hgt_close = 0.0
    sgt_close = 0.0
    got_realtime = False

    # Source 1: 同花顺 hsgtApi (primary)
    try:
        hsgt_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "Chrome/117.0.0.0 Safari/537.36"
            ),
            "Host": "data.hexin.cn",
            "Referer": "https://data.hexin.cn/",
        }
        url_rt = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
        r = _requests.get(url_rt, headers=hsgt_headers, timeout=10,
                          proxies=_NO_PROXY)
        d = r.json()

        times = d.get("time", [])
        hgt = d.get("hgt", [])
        sgt = d.get("sgt", [])

        if times:
            lines.append("## Realtime (cumulative net buying, 亿元, 同花顺)")
            n = len(times)
            start_idx = max(0, n - 10)
            for i in range(start_idx, n):
                t = times[i]
                h = hgt[i] if i < len(hgt) else "N/A"
                s = sgt[i] if i < len(sgt) else "N/A"
                lines.append(f"  {t}: HGT={h} SGT={s}")

            hgt_close = float(hgt[-1]) if hgt else 0
            sgt_close = float(sgt[-1]) if sgt else 0
            total = hgt_close + sgt_close
            lines.append(
                f"\nClose: HGT(沪股通)={hgt_close:.2f}亿 "
                f"SGT(深股通)={sgt_close:.2f}亿 "
                f"Total={total:.2f}亿"
            )
            if total > 0:
                lines.append("Signal: Net northbound INFLOW (bullish)")
            elif total < 0:
                lines.append("Signal: Net northbound OUTFLOW (bearish)")
            got_realtime = True
        else:
            lines.append("No realtime data from 同花顺 (non-trading hours or holiday)")

        if got_realtime:
            today_str = datetime.now().strftime("%Y-%m-%d")
            _save_northbound_snapshot(today_str, hgt_close, sgt_close)

        lines.append("\n# Source: 同花顺 hsgtApi")

    except Exception as e:
        logger.warning("同花顺北向资金 failed: %s", e)

    # Source 2: 东财 push2 kamt (fallback)
    if not got_realtime:
        try:
            url_kamt = "http://push2his.eastmoney.com/api/qt/kamt.rtmin/get"
            params_kamt = {
                "fields1": "f1,f2,f3,f4",
                "fields2": "f51,f52,f53,f54,f55,f56",
            }
            r2 = _requests.get(url_kamt, params=params_kamt,
                               headers={"User-Agent": _UA}, timeout=10,
                               proxies=_NO_PROXY)
            d2 = r2.json()
            s2n = d2.get("data", {}).get("s2n", [])

            if s2n:
                lines.append("## Realtime (cumulative net buying, 亿元, 东财)")
                for line in s2n[-10:]:
                    parts = line.split(",")
                    if len(parts) >= 3:
                        # parts: time, sgt_net, hgt_net, ...
                        lines.append(
                            f"  {parts[0]}: HGT={parts[2]} SGT={parts[1]}"
                        )
                # Last values
                last = s2n[-1].split(",")
                if len(last) >= 3:
                    try:
                        hgt_close = float(last[2])
                        sgt_close = float(last[1])
                        total = hgt_close + sgt_close
                        lines.append(
                            f"\nClose: HGT(沪股通)={hgt_close:.2f}亿 "
                            f"SGT(深股通)={sgt_close:.2f}亿 "
                            f"Total={total:.2f}亿"
                        )
                        if total > 0:
                            lines.append("Signal: Net northbound INFLOW (bullish)")
                        elif total < 0:
                            lines.append("Signal: Net northbound OUTFLOW (bearish)")
                    except (ValueError, IndexError):
                        pass
                got_realtime = True
                lines.append("\n# Source: 东财 push2 (同花顺不可用时的备选)")

                if got_realtime:
                    today_str = datetime.now().strftime("%Y-%m-%d")
                    _save_northbound_snapshot(today_str, hgt_close, sgt_close)
        except Exception as e:
            logger.warning("东财北向资金 failed: %s", e)

    # Historical daily close (local cache)
    if include_history:
        history = _load_northbound_history(20)
        if history:
            lines.append("\n## Historical Daily Close (local cache, 亿元)")
            lines.append("Date       | HGT(沪股通) | SGT(深股通) | Total")
            for date, h, s in history:
                lines.append(f"  {date}: HGT={h:.2f} SGT={s:.2f} Total={h + s:.2f}")
            avg_total = sum(h + s for _, h, s in history) / len(history)
            lines.append(
                f"\n{len(history)}-day avg net flow: {avg_total:.2f}亿"
            )
            if got_realtime:
                today_total = hgt_close + sgt_close
                diff = today_total - avg_total
                lines.append(
                    f"Today vs avg: {'+' if diff >= 0 else ''}{diff:.2f}亿 "
                    f"({'above' if diff >= 0 else 'below'} average)"
                )
        else:
            lines.append(
                "\n## Historical Daily: No cached data yet. "
                "History accumulates automatically with each call."
            )

    if not lines or (len(lines) <= 2 and not got_realtime):
        return f"Error fetching northbound flow: all sources failed"

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Baidu PAE (百度股市通) helpers
# ---------------------------------------------------------------------------

_BAIDU_PAE_HEADERS = {
    "Host": "finance.pae.baidu.com",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) "
        "Gecko/20100101 Firefox/110.0"
    ),
    "Accept": "application/vnd.finance-web.v1+json",
    "Origin": "https://gushitong.baidu.com",
    "Referer": "https://gushitong.baidu.com/",
}


# ---- 13. get_concept_blocks ----


def get_concept_blocks(
    ticker: Annotated[str, "A-stock code (e.g. 688017)"],
) -> str:
    """Get concept/sector/region blocks that a stock belongs to.

    Primary: 百度股市通 PAE; Fallback: 东财 emweb (industry info).
    Returns industry classification (东财/申万), concept themes, and region.
    """
    code = _normalize_ticker(ticker)
    market = "SZ" if code.startswith(("0", "3")) else "SH"
    lines = [
        f"# Concept & Sector Blocks for {code} (A-stock)",
        f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    got_data = False

    # Source 1: 百度 PAE (primary)
    try:
        url = (
            "https://finance.pae.baidu.com/api/getrelatedblock"
            f'?stock=[{{"code":"{code}","market":"ab","type":"stock"}}]'
            "&finClientType=pc"
        )
        r = _requests.get(url, headers=_BAIDU_PAE_HEADERS, timeout=10,
                         proxies=_NO_PROXY)
        d = r.json()

        if str(d.get("ResultCode", -1)) == "0":
            result = d.get("Result", {})
            categories = result.get(code, [])
            concept_names: list[str] = []

            for cat in categories:
                cat_name = cat.get("name", "")
                items = cat.get("list", [])
                if not items:
                    continue
                lines.append(f"## {cat_name}")
                for item in items:
                    name = item.get("name", "")
                    ratio = item.get("ratio", "")
                    desc = item.get("describe", "")
                    suffix = f" ({desc})" if desc else ""
                    lines.append(f"  {name}{suffix}: {ratio}")
                    if cat_name == "概念":
                        concept_names.append(name)

            if concept_names:
                lines.append(f"\nConcept tags: {' / '.join(concept_names)}")
            lines.append("\n# Source: 百度股市通 (Baidu PAE)")
            got_data = True
    except Exception:
        pass

    # Source 2: 东财 emweb (fallback — industry info)
    if not got_data:
        try:
            emw_url = (
                f"http://emweb.securities.eastmoney.com"
                f"/PC_HSF10/CompanySurvey/PageAjax?code={market}{code}"
            )
            r = _requests.get(emw_url, timeout=10, proxies=_NO_PROXY)
            d = r.json()
            jbzl_list = d.get("jbzl", [])
            if jbzl_list:
                jbzl = jbzl_list[0] if isinstance(jbzl_list, list) else jbzl_list
                em_industry = jbzl.get("EM2016", "")
                csrc_industry = jbzl.get("INDUSTRYCSRC1", "")
                province = jbzl.get("PROVINCE", "")
                org_profile = jbzl.get("ORG_PROFILE", "")

                if em_industry:
                    lines.append("## 行业 (东财)")
                    for level in em_industry.split("-"):
                        lines.append(f"  {level}")
                if csrc_industry:
                    lines.append("\n## 行业 (申万)")
                    for level in csrc_industry.split("-"):
                        lines.append(f"  {level}")
                if province:
                    lines.append(f"\n## 地区")
                    lines.append(f"  {province}")
                if org_profile:
                    lines.append("\n## 公司简介")
                    lines.append(f"  {org_profile[:200]}")
                lines.append("\n# Source: 东财 emweb (Baidu PAE 不可用时的备选)")
                got_data = True
        except Exception:
            pass

    if not got_data:
        raise ValueError(f"No concept/block data available for {code}")

    return "\n".join(lines)

def get_fund_flow(
    ticker: Annotated[str, "A-stock code"],
    curr_date: Annotated[str, "Date YYYY-MM-DD"],
    include_history: Annotated[
        bool, "Include historical daily fund flow (last 20 days)"
    ] = True,
) -> str:
    """Get individual stock fund flow from 东财 push2 (primary) or 腾讯 (fallback).

    Realtime: minute-level main/large/medium/small/super order net inflow.
    History: daily net inflow for 20 trading days (push2his).

    V0.2.7: replaced 百度 PAE (fundflow/fundsortlist, offline since 2026-05)
    with 东财 push2 fund flow API.
    """
    code = _normalize_ticker(ticker)
    secid = f"1.{code}" if code.startswith("6") else f"0.{code}"
    lines = [
        f"# Fund Flow for {code} (A-stock)",
        f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    got_data = False

    _push2_headers = {"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"}

    # Source 1: 东财 push2 (primary)
    try:
        # Realtime minute-level fund flow
        url_rt = "http://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
        params_rt = {
            "secid": secid, "klt": 1,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
        }
        r = _requests.get(url_rt, params=params_rt, headers=_push2_headers, timeout=10,
                          proxies=_NO_PROXY)
        d = r.json()
        klines = d.get("data", {}).get("klines", [])

        if klines:
            lines.append(
                "## Realtime Minute Flow "
                "(主力/小单/中单/大单/超大单 净流入, 元)"
            )
            for line in klines[-10:]:
                parts = line.split(",")
                if len(parts) >= 6:
                    lines.append(
                        f"  {parts[0]}: "
                        f"主力={float(parts[1])/1e4:.0f}万 "
                        f"大单={float(parts[4])/1e4:.0f}万 "
                        f"超大单={float(parts[5])/1e4:.0f}万"
                    )

            last_parts = klines[-1].split(",")
            if len(last_parts) >= 2:
                main_net = float(last_parts[1])
                lines.append(
                    f"\nClose: 主力净流入={main_net/1e4:.0f}万元"
                )
                if main_net > 0:
                    lines.append(
                        "Signal: Net main force INFLOW (bullish)"
                    )
                elif main_net < 0:
                    lines.append(
                        "Signal: Net main force OUTFLOW (bearish)"
                    )
            got_data = True
        else:
            # 非交易时间或有数据但 klines 为空
            d_check = d.get("data", {})
            if d_check and d_check.get("code") is not None:
                lines.append(
                    "No realtime fund flow (non-trading hours or holiday)"
                )
                got_data = True  # push2 可达只是非交易时间

        # Historical daily fund flow (push2his)
        if include_history:
            url_hist = (
                "http://push2his.eastmoney.com"
                "/api/qt/stock/fflow/daykline/get"
            )
            params_hist = {
                "secid": secid, "lmt": 20, "klt": 101,
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57",
            }
            rh = _requests.get(
                url_hist, params=params_hist, headers=_push2_headers, timeout=10,
                proxies=_NO_PROXY
            )
            dh = rh.json()
            hist_klines = dh.get("data", {}).get("klines", [])

            if hist_klines:
                lines.append(
                    f"\n## Historical Daily Fund Flow "
                    f"(last {len(hist_klines)} trading days)"
                )
                lines.append(
                    "Date | 主力净流入(万) | 大单(万) "
                    "| 中单(万) | 小单(万) | 超大单(万)"
                )
                for line in hist_klines:
                    parts = line.split(",")
                    if len(parts) >= 6:
                        lines.append(
                            f"  {parts[0]} "
                            f"| main={float(parts[1])/1e4:.0f} "
                            f"| large={float(parts[4])/1e4:.0f} "
                            f"| mid={float(parts[3])/1e4:.0f} "
                            f"| small={float(parts[2])/1e4:.0f} "
                            f"| super={float(parts[5])/1e4:.0f}"
                        )

        if got_data or klines:
            lines.append("\n# Source: 东财 push2")
            return "\n".join(lines)

    except Exception as e:
        logger.warning("东财 push2 fund flow failed for %s: %s", code, e)

    # Source 2: 腾讯 — 利用实时行情提供换手率/成交量/涨跌幅作为资金概览 (fallback)
    if not got_data:
        try:
            tq = _tencent_quote([code])
            if code in tq:
                q = tq[code]
                lines.append("## Fund Flow Overview (腾讯，东财不可用时的备选)")
                lines.append(f"  Name: {q['name']}")
                lines.append(f"  Price: {q['price']}")
                lines.append(f"  Change: {q['change_pct']}%")
                lines.append(f"  Turnover Rate: {q['turnover_pct']}%")
                lines.append(f"  Market Cap: {q['mcap_yi']}亿")
                lines.append(f"  Float Market Cap: {q['float_mcap_yi']}亿")
                lines.append(f"  PE (TTM): {q['pe_ttm']}")
                lines.append(f"  PB: {q['pb']}")
                lines.append("\n# Source: 腾讯实时行情 (东财push2不可用时的备选)")
                got_data = True
        except Exception as e:
            logger.warning("腾讯 fund flow fallback failed for %s: %s", code, e)

    if not got_data:
        return f"Error fetching fund flow for {code}: all sources failed"

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# 15. Dragon Tiger Board (龙虎榜)
# ---------------------------------------------------------------------------

def get_dragon_tiger_board(
    ticker: str,
    trade_date: str,
    look_back_days: int = 30,
) -> str:
    """Get dragon-tiger board (龙虎榜) appearances and seat details.

    Primary: 东财 datacenter (RPT_DAILYBILLBOARD_DETAILSNEW);
    Fallback: mootdx F10 龙虎榜.

    Args:
        ticker: 6-digit A-share code, e.g. '000858'
        trade_date: YYYY-MM-DD
        look_back_days: how many days back to search (default 30)

    Returns:
        Formatted text with LHB appearances, top buyer/seller seats,
        and institutional activity.
    """
    code = safe_ticker_component(ticker)
    end_dt = datetime.strptime(trade_date, "%Y-%m-%d")
    start_dt = end_dt - pd.Timedelta(days=look_back_days)
    start_date_str = start_dt.strftime("%Y-%m-%d")
    lines = [f"# 龙虎榜数据 | {code} | {trade_date} (近{look_back_days}日)"]

    buy_data = None
    sell_data = None
    data = None
    got_data = False

    # Source 1: 上榜记录 — eastmoney datacenter direct HTTP (primary)
    try:
        data = _eastmoney_datacenter(
            "RPT_DAILYBILLBOARD_DETAILSNEW",
            filter_str=(
                f"(TRADE_DATE>='{start_date_str}')"
                f"(TRADE_DATE<='{trade_date}')"
                f"(SECURITY_CODE=\"{code}\")"
            ),
            page_size=50,
            sort_columns="TRADE_DATE",
            sort_types="-1",
        )
        if not data:
            lines.append(f"\n近{look_back_days}日未上龙虎榜。")
        else:
            lines.append(f"\n## 上榜记录 ({len(data)} 次)")
            lines.append("日期 | 原因 | 净买入(万) | 换手率")
            for row in data:
                net_buy = round((row.get("BILLBOARD_NET_AMT") or 0) / 10000, 1)
                turnover = round(float(row.get("TURNOVERRATE") or 0), 2)
                lines.append(
                    f"  {str(row.get('TRADE_DATE', ''))[:10]} "
                    f"| {row.get('EXPLANATION', '')} "
                    f"| {net_buy:.0f} "
                    f"| {turnover:.2f}%"
                )
    except Exception as e:
        lines.append(f"龙虎榜列表查询失败: {e}")

    # 2. 最近上榜的买卖席位 — eastmoney datacenter direct HTTP
    try:
        if data:
            latest_date = str(data[0].get("TRADE_DATE", ""))[:10]
            lines.append(f"\n## 最近上榜席位明细 ({latest_date})")

            # 买入席位
            buy_data = _eastmoney_datacenter(
                "RPT_BILLBOARD_DAILYDETAILSBUY",
                filter_str=f"(TRADE_DATE='{latest_date}')(SECURITY_CODE=\"{code}\")",
                page_size=10,
                sort_columns="BUY",
                sort_types="-1",
            )
            if buy_data:
                lines.append("\n### 买入席位 TOP5")
                lines.append("营业部 | 买入(万) | 卖出(万) | 净额(万)")
                for row in buy_data[:5]:
                    buy_amt = round((row.get("BUY") or 0) / 10000, 1)
                    sell_amt = round((row.get("SELL") or 0) / 10000, 1)
                    net = round((row.get("NET") or 0) / 10000, 1)
                    lines.append(
                        f"  {row.get('OPERATEDEPT_NAME', '')} "
                        f"| {buy_amt:.0f} | {sell_amt:.0f} | {net:.0f}"
                    )

            # 卖出席位
            sell_data = _eastmoney_datacenter(
                "RPT_BILLBOARD_DAILYDETAILSSELL",
                filter_str=f"(TRADE_DATE='{latest_date}')(SECURITY_CODE=\"{code}\")",
                page_size=10,
                sort_columns="SELL",
                sort_types="-1",
            )
            if sell_data:
                lines.append("\n### 卖出席位 TOP5")
                lines.append("营业部 | 买入(万) | 卖出(万) | 净额(万)")
                for row in sell_data[:5]:
                    buy_amt = round((row.get("BUY") or 0) / 10000, 1)
                    sell_amt = round((row.get("SELL") or 0) / 10000, 1)
                    net = round((row.get("NET") or 0) / 10000, 1)
                    lines.append(
                        f"  {row.get('OPERATEDEPT_NAME', '')} "
                        f"| {buy_amt:.0f} | {sell_amt:.0f} | {net:.0f}"
                    )
    except Exception:
        pass

    # 3. 机构动向 — 从买卖席位明细筛选机构专用席位
    try:
        inst_buy = 0.0
        inst_sell = 0.0
        for detail, side in [(buy_data, "buy"), (sell_data, "sell")]:
            for row in (detail or []):
                if str(row.get("OPERATEDEPT_CODE", "")) == "0":
                    if side == "buy":
                        inst_buy += (row.get("BUY") or 0)
                    else:
                        inst_sell += (row.get("SELL") or 0)
        if inst_buy > 0 or inst_sell > 0:
            lines.append("\n## 机构动向")
            lines.append(
                f"  机构买入 {inst_buy/1e4:.0f} 万 "
                f"| 卖出 {inst_sell/1e4:.0f} 万 "
                f"| 净额 {(inst_buy - inst_sell)/1e4:.0f} 万"
            )
    except Exception:
        pass

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 16. Lockup Expiry Calendar (限售解禁日历)
# ---------------------------------------------------------------------------

def get_lockup_expiry(
    ticker: str,
    trade_date: str,
    forward_days: int = 90,
) -> str:
    """Get lockup expiry schedule for a stock.

    Args:
        ticker: 6-digit A-share code
        trade_date: YYYY-MM-DD
        forward_days: how many days forward to check (default 90)

    Returns:
        Formatted text with historical unlock records and upcoming
        expiry calendar with impact metrics.
    """
    code = safe_ticker_component(ticker)
    lines = [f"# 限售解禁日历 | {code} | {trade_date}"]

    # 1. 历史解禁记录 — eastmoney datacenter direct HTTP
    try:
        history_data = _eastmoney_datacenter(
            "RPT_LIFT_STAGE",
            filter_str=f"(SECURITY_CODE=\"{code}\")",
            page_size=15,
            sort_columns="FREE_DATE",
            sort_types="-1",
        )
        if history_data:
            lines.append(f"\n## 个股解禁记录 (共 {len(history_data)} 批)")
            lines.append("解禁时间 | 类型 | 解禁数量 | 占比")
            for row in history_data:
                lines.append(
                    f"  {str(row.get('FREE_DATE', ''))[:10]} "
                    f"| {row.get('LIMITED_STOCK_TYPE', '')} "
                    f"| {row.get('FREE_SHARES_NUM', '')} "
                    f"| {row.get('FREE_RATIO', '')}"
                )
        else:
            lines.append("\n无历史解禁记录。")
    except Exception as e:
        lines.append(f"个股解禁查询失败: {e}")

    # 2. 未来待解禁 — eastmoney datacenter direct HTTP
    try:
        end_dt = datetime.strptime(trade_date, "%Y-%m-%d") + pd.Timedelta(
            days=forward_days
        )
        end_str = end_dt.strftime("%Y-%m-%d")
        upcoming_data = _eastmoney_datacenter(
            "RPT_LIFT_STAGE",
            filter_str=(
                f"(SECURITY_CODE=\"{code}\")"
                f"(FREE_DATE>='{trade_date}')"
                f"(FREE_DATE<='{end_str}')"
            ),
            page_size=20,
            sort_columns="FREE_DATE",
            sort_types="1",
        )
        if upcoming_data:
            lines.append(f"\n## 未来 {forward_days} 天待解禁")
            for row in upcoming_data:
                lines.append(
                    f"  {str(row.get('FREE_DATE', ''))[:10]} "
                    f"| {row.get('LIMITED_STOCK_TYPE', '')} "
                    f"| 数量 {row.get('FREE_SHARES_NUM', '')} "
                    f"| 占比 {row.get('FREE_RATIO', '')}"
                )
        else:
            lines.append(f"\n未来 {forward_days} 天无待解禁。")
    except Exception as e:
        lines.append(f"解禁日历查询失败: {e}")


    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 17. Industry Comparison (行业横向对比)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 17. Industry Comparison (行业横向对比)
# ---------------------------------------------------------------------------

def get_industry_comparison(
    ticker: str,
    trade_date: str,
    top_n: int = 20,
) -> str:
    """Get industry sector performance comparison.

    Args:
        ticker: 6-digit A-share code (used to identify relevant sector)
        trade_date: YYYY-MM-DD
        top_n: number of top/bottom industries to show (default 20)

    Returns:
        Formatted text with sector performance ranking, highlighting
        the sector the target stock belongs to.
    """
    code = safe_ticker_component(ticker)
    lines = [f"# 行业横向对比 | {code} | {trade_date}"]

    # 东财 push2 行业板块排名 (direct HTTP, replaces 同花顺 which has 401)
    try:
        url = "http://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1",
            "pz": "100",
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fs": "m:90+t:2",
            "fields": "f2,f3,f4,f12,f13,f14,f104,f105,f128,f136,f140,f141,f207",
        }
        r = _requests.get(url, params=params, headers={"User-Agent": _UA}, timeout=15,
                          proxies=_NO_PROXY)
        d = r.json()
        items = d.get("data", {}).get("diff", [])

        if items:
            lines.append(
                f"\n## 全行业表现 (东财 {len(items)} 个行业)"
            )
            lines.append(
                "排名 | 行业 | 涨跌幅 | 上涨 | 下跌 | 领涨股"
            )
            for i, item in enumerate(items):
                name = item.get("f14", "")
                change_pct = item.get("f3", 0)
                up_count = item.get("f104", 0)
                down_count = item.get("f105", 0)
                leader = item.get("f140", "")
                lines.append(
                    f"  {i+1}. {name} "
                    f"| {change_pct}% "
                    f"| {up_count} "
                    f"| {down_count} "
                    f"| {leader}"
                )
                if i >= top_n * 2 - 1:
                    lines.append(f"  ... (showing top/bottom {top_n})")
                    break
        else:
            lines.append("行业数据获取为空。")
    except Exception as e:
        lines.append(f"行业对比查询失败: {e}")

    return "\n".join(lines)


# ===========================================================================
# 10 缠论分析
# ===========================================================================


def get_chanlun_analysis(
    symbol: Annotated[str, "6-digit A-stock code"],
    curr_date: Annotated[str, "Current trading date, YYYY-mm-dd"],
    look_back_days: Annotated[int, "Days of daily data to look back"] = 2000,
    level: Annotated[str, "K-line level: daily/weekly/monthly"] = "daily",
    start_date: Annotated[str, "Start date YYYY-MM-DD, empty=from config"] = "",
) -> str:
    """缠论综合分析 — 返回中枢、走势类型、背驰、买卖点的完整文本报告。

    基于「缠中说禅」理论，对A股标的进行缠论分析。
    分析内容：K线包含处理→分型识别→笔划分→线段→中枢→走势类型→背驰→买卖点。

    需要本地缓存中有足够的K线数据（使用数据同步功能可获取）。
    """
    from .chanlun import analyze, format_result

    code = _normalize_ticker(symbol)

    # Resolve start_date
    if not start_date:
        from .config import get_config
        config = get_config()
        start_date = config.get("kline_start_date", "2024-01-01")

    # 优先从 SQLite 缓存加载
    df = _db_load(code, level, data_type="stock",
                  start_date=start_date, end_date=curr_date)

    # 没有缓存或缓存为空，尝试在线获取
    if df.empty:
        try:
            df, _ = _fetch_kline(code, level=level, start_date=start_date, datalen=10000)
            if not df.empty:
                cutoff = pd.to_datetime(curr_date)
                df = df[df["Date"] <= cutoff]
            else:
                # 对周线/月线尝试日线重采样
                if level in ("weekly", "monthly"):
                    df_daily, _ = _fetch_kline(code, level="daily", start_date=start_date, datalen=10000)
                    if not df_daily.empty:
                        cutoff = pd.to_datetime(curr_date)
                        df_daily = df_daily[df_daily["Date"] <= cutoff]
                        df = _resample_kline(df_daily, level)
        except Exception as e:
            return f"缠论分析失败：无法获取 {symbol} 的K线数据（{e}）。请先在「数据同步」页面同步该股票数据。"

    if df.empty:
        return f"缠论分析失败：{symbol} 无可用的K线数据。请先在「数据同步」页面同步该股票数据。"

    # 截取最近 look_back_days 天数据
    if len(df) > look_back_days:
        df = df.iloc[-look_back_days:]

    # 运行缠论分析
    try:
        result = analyze(df, symbol=code, curr_date=curr_date, level=level)
        return format_result(result)
    except Exception as e:
        logger.error("缠论分析异常 %s: %s", code, e)
        return f"缠论分析异常：{e}"
