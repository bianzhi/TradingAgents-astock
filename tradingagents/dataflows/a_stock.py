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
import uuid
import urllib.request

import pandas as pd
import requests as _requests

from .utils import safe_ticker_component

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers: ticker format & market detection
# ---------------------------------------------------------------------------

def _get_prefix(code: str) -> str:
    """6-digit A-stock code -> market prefix for Tencent API."""
    if code.startswith(("6", "9")):
        return "sh"
    elif code.startswith("8"):
        return "bj"
    return "sz"


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
    """Build name→code and code→name maps via mootdx (both SH & SZ markets)."""
    global _name_to_code, _code_to_name
    if _name_to_code is not None:
        return _name_to_code, _code_to_name

    from mootdx.quotes import Quotes

    client = Quotes.factory(market="std")
    n2c: dict[str, str] = {}
    c2n: dict[str, str] = {}

    for market in (0, 1):  # 0=SZ, 1=SH
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

    _name_to_code = n2c
    _code_to_name = c2n
    logger.info("Built stock name-code map: %d entries", len(n2c))
    return _name_to_code, _code_to_name


def resolve_ticker(user_input: str) -> str:
    """Resolve user input (code or Chinese name) to a 6-digit A-stock code.

    Accepts: '600379', 'SH600379', '600379.SH', '宝光股份'
    Returns: '600379'
    Raises: ValueError if not resolvable.
    """
    s = user_input.strip()
    if not s:
        raise ValueError("输入不能为空")

    has_chinese = any("一" <= ch <= "鿿" for ch in s)

    if not has_chinese:
        return _normalize_ticker(s)

    clean = s.replace(" ", "").replace("　", "")
    n2c, _ = _build_name_code_map()

    if clean in n2c:
        return n2c[clean]

    matches = {name: code for name, code in n2c.items() if clean in name}
    if len(matches) == 1:
        return next(iter(matches.values()))
    if len(matches) > 1:
        examples = ", ".join(f"{n}({c})" for n, c in list(matches.items())[:5])
        raise ValueError(f"'{s}' 匹配到多只股票: {examples}，请输入完整名称或代码")

    raise ValueError(f"找不到股票 '{s}'，请检查名称是否正确")


# ---------------------------------------------------------------------------
# mootdx client (singleton)
# ---------------------------------------------------------------------------

_mootdx_client = None


def _get_mootdx_client():
    """Lazy-init mootdx Quotes client (TCP connection, reusable)."""
    global _mootdx_client
    if _mootdx_client is None:
        from mootdx.quotes import Quotes

        _mootdx_client = Quotes.factory(market="std")
    return _mootdx_client


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
        _DATACENTER_URL, params=params, headers={"User-Agent": _UA}, timeout=15
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
    r = _requests.get(url, headers=headers, timeout=15)
    r.encoding = "gbk"
    dfs = pd.read_html(r.text)
    # Find the table containing EPS data
    for df in dfs:
        cols = [str(c) for c in df.columns]
        if any("每股收益" in c or "均值" in c for c in cols):
            return df
    # Fallback: return first table if exists
    return dfs[0] if dfs else pd.DataFrame()


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
_KLINE_FALLBACK_MINUTE = ["eastmoney", "sina", "tencent", "tushare"]
_KLINE_FALLBACK_DAILY = ["sina", "tencent", "tushare", "eastmoney"]


def _eastmoney_kline(code: str, level: str = "daily",
                     start_date: str = None, datalen: int = 10000) -> pd.DataFrame:
    """从东方财富获取K线数据（支持1分~日线）。

    东方财富 push2his K线接口，日线最多10000根，分钟级最优。

    Returns:
        DataFrame with columns: Date, Open, High, Low, Close, Volume.
    """
    secid = f"1.{code}" if code.startswith("6") else f"0.{code}"
    klt = _EASTMONEY_KLT.get(level, 101)

    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid,
        "klt": str(klt),
        "fqt": "1",  # 前复权
        "lmt": str(datalen),
        "end": "20500101",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
    }

    try:
        r = _requests.get(url, params=params, headers={"User-Agent": _UA}, timeout=30)
        r.raise_for_status()
    except Exception as e:
        # SSL errors common in some environments — try without verify
        try:
            r = _requests.get(url, params=params, headers={"User-Agent": _UA},
                              timeout=30, verify=False)
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
    prefix = "sh" if code.startswith("6") else "sz"
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
    r = _requests.get(url, params=params, timeout=30)
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
    prefix = "sh" if code.startswith("6") else "sz"
    # 腾讯 K线 type: 1=1分, 5=5分, 15=15分, 30=30分, 60=60分, day=日线, week=周线
    _TENCENT_TYPE = {
        "1min": "1", "5min": "5", "15min": "15", "30min": "30", "60min": "60",
        "daily": "day", "weekly": "week",
    }
    ktype = _TENCENT_TYPE.get(level, "day")

    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {
        "_var": f"kline_{ktype}",
        "param": f"{prefix}{code},{ktype},,{datalen},1,qfq",
    }

    try:
        r = _requests.get(url, params=params, timeout=30)
        r.raise_for_status()
    except Exception as e:
        # SSL errors — retry without verify
        try:
            r = _requests.get(url, params=params, timeout=30, verify=False)
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

    # 提取数据: d["data"][prefix+code]["qfqday"] 或 ["qfqweek"]
    raw_data = d.get("data", {})
    if isinstance(raw_data, list):
        # Some responses return data as list — not usable
        return pd.DataFrame()

    stock_data = raw_data.get(prefix + code, {})
    # 前复权数据优先
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
            continue

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
    """将日线K线重采样为周线/月线。

    Args:
        df: 日线 DataFrame，需包含 Date/Open/High/Low/Close/Volume 列
        target_level: "weekly" 或 "monthly"

    Returns:
        重采样后的 DataFrame
    """
    if df.empty or target_level not in ("weekly", "monthly"):
        return df

    df = df.copy()
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


def _daily_cache_path(code: str, level: str = "daily") -> str:
    """Return the K-line CSV path for a stock code and level."""
    return os.path.join(_cache_dir(), f"{code}-astock-{level}.csv")


def _daily_meta_path(code: str, level: str = "daily") -> str:
    """Return the K-line metadata JSON path."""
    return os.path.join(_cache_dir(), f"{code}-astock-{level}-meta.json")


def sync_stock_data(code: str, level: str = "daily",
                    datalen: int = 10000, start_date: str = None,
                    sources: list[str] | None = None) -> dict:
    """全量同步单只股票的K线数据到本地缓存。四大数据源自动协同，主源失败自动切换备源。

    数据源与级别支持:
        - 东方财富 (1分~日线, 日线最多10000根, 分钟级最优)
        - 新浪财经 (1分~日线, 速度快, 最多2000根)
        - 腾讯财经 (1分~周线, 稳定, 最多2000根)
        - Tushare  (1分~月线, 数据质量最高, 需 token)

    同步策略:
        - 分钟级: 东方财富 → 新浪 → 腾讯 → Tushare
        - 日线/周线/月线: 新浪 → 腾讯 → Tushare → 东方财富
        - 周线/月线: 优先直接获取，失败则从日线重采样确保完整性
        - 全部失败: mootdx TCP 兜底 (最多800根)

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
            "error": None | "error message"
        }
    """
    code = _normalize_ticker(code)
    cache_file = _daily_cache_path(code, level)
    meta_file = _daily_meta_path(code, level)

    # Resolve start_date
    if not start_date:
        from .config import get_config
        config = get_config()
        start_date = config.get("kline_start_date", "2024-01-01")

    # Get K-line data using multi-source fetcher
    df, source_name = _fetch_kline(
        code, level=level, start_date=start_date,
        datalen=datalen, sources=sources,
    )

    # 周线/月线：尝试直接获取失败后，从日线重采样
    if df.empty and level in ("weekly", "monthly"):
        logger.info("Direct %s fetch failed for %s, trying daily resample", level, code)
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
        }

    # Save CSV
    df.to_csv(cache_file, index=False, encoding="utf-8")

    # Save meta
    import json as _json_mod
    meta = {
        "code": code,
        "level": level,
        "rows": len(df),
        "date_range": [
            df["Date"].min().strftime("%Y-%m-%d"),
            df["Date"].max().strftime("%Y-%m-%d"),
        ],
        "last_sync": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": source_name,
        "datalen": datalen,
        "start_date": start_date,
    }
    with open(meta_file, "w", encoding="utf-8") as f:
        _json_mod.dump(meta, f, ensure_ascii=False, indent=2)

    return {
        "code": code,
        "level": level,
        "rows": len(df),
        "date_range": meta["date_range"],
        "source": source_name,
        "status": "ok",
        "error": None,
    }


def get_cached_stocks() -> list[dict]:
    """列出所有已缓存的股票及其元信息。

    Returns:
        [{"code": "300750", "level": "daily", "rows": 3200, ...}, ...]
    """
    cache_dir = _cache_dir()
    import json as _json_mod

    results = []
    if not os.path.exists(cache_dir):
        return results

    for meta_file in sorted(os.listdir(cache_dir)):
        if not meta_file.endswith("-meta.json") or "-astock-" not in meta_file:
            continue
        meta_path = os.path.join(cache_dir, meta_file)
        # Infer level from filename: {code}-astock-{level}-meta.json
        base = meta_file.replace("-meta.json", "")
        parts = base.split("-astock-")
        inferred_level = parts[1] if len(parts) == 2 else "daily"

        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = _json_mod.load(f)
            # Backfill missing fields for old caches
            if "level" not in meta:
                meta["level"] = inferred_level
            if "start_date" not in meta:
                meta["start_date"] = ""
            results.append(meta)
        except Exception:
            # Fallback: infer from CSV
            if len(parts) == 2:
                code, level = parts
                csv_path = os.path.join(cache_dir, f"{code}-astock-{level}.csv")
                if os.path.exists(csv_path):
                    try:
                        df = pd.read_csv(csv_path, encoding="utf-8", on_bad_lines="skip")
                        mtime = os.path.getmtime(csv_path)
                        results.append({
                            "code": code,
                            "level": level,
                            "rows": len(df),
                            "date_range": [df.iloc[0]["Date"], df.iloc[-1]["Date"]] if len(df) > 0 else [],
                            "last_sync": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
                            "source": "unknown",
                            "start_date": "",
                        })
                    except Exception:
                        pass

    return results


def delete_cache(code: str, level: str | None = None) -> bool:
    """删除指定股票的缓存数据。

    Args:
        code: 6位股票代码
        level: K线级别，None则删除所有级别

    Returns:
        True if any file was deleted, False otherwise.
    """
    deleted = False
    if level:
        for path in [_daily_cache_path(code, level), _daily_meta_path(code, level)]:
            if os.path.exists(path):
                os.remove(path)
                deleted = True
    else:
        # Delete all levels for this code
        cache_dir = _cache_dir()
        if os.path.exists(cache_dir):
            for f in os.listdir(cache_dir):
                if f.startswith(f"{code}-astock-") and (
                    f.endswith(".csv") or f.endswith("-meta.json")
                ):
                    os.remove(os.path.join(cache_dir, f))
                    deleted = True
    return deleted


def _load_ohlcv_astock(symbol: str, curr_date: str) -> pd.DataFrame:
    """Fetch OHLCV daily data, using cached/sync data first, then multi-source fetch.

    Mirrors stockstats_utils.load_ohlcv but uses A-stock data sources.
    Returns DataFrame with columns: Date, Open, High, Low, Close, Volume
    """
    code = _normalize_ticker(symbol)
    cache_file = _daily_cache_path(code, "daily")

    if os.path.exists(cache_file):
        # If cached file exists, use it (may have more data from sync_stock_data)
        # Use cached data if it exists at all — sync_stock_data provides full history
        data = pd.read_csv(cache_file, on_bad_lines="skip", encoding="utf-8")
        data["Date"] = pd.to_datetime(data["Date"])
        cutoff = pd.to_datetime(curr_date)
        return data[data["Date"] <= cutoff]

    # No cache — fetch from multi-source API (with mootdx final fallback)
    df, _ = _fetch_kline(code, level="daily", start_date=None, datalen=10000)
    if df.empty:
        # Final mootdx TCP fallback
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
        except Exception as e:
            raise ValueError(f"No OHLCV data available for {code}: {e}")

    if df.empty:
        raise ValueError(f"No OHLCV data available for {code}")

    # Cache to disk
    os.makedirs(os.path.dirname(cache_file), exist_ok=True)
    df.to_csv(cache_file, index=False, encoding="utf-8")

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
        return (
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
        try:
            market_code = 1 if code.startswith("6") else 0
            _info_url = "https://push2.eastmoney.com/api/qt/stock/get"
            _info_params = {
                "fltt": "2",
                "invt": "2",
                "fields": "f57,f58,f84,f85,f127,f116,f117,f189,f43",
                "secid": f"{market_code}.{code}",
            }
            r = _requests.get(
                _info_url, params=_info_params,
                headers={"User-Agent": _UA}, timeout=10,
            )
            d = r.json().get("data", {})
            if d:
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
            return f"No fundamentals data found for A-stock '{code}'"

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
    r = _requests.get(url, params=params, headers={"User-Agent": _UA}, timeout=15)
    d = r.json()

    result = d.get("result", {}).get("data", {})
    items = result.get(source_type, [])
    if not isinstance(items, list) or not items:
        return pd.DataFrame()

    df = pd.DataFrame(items)

    # Filter by curr_date
    if curr_date and "报告日" in df.columns:
        df["报告日"] = pd.to_datetime(df["报告日"], errors="coerce")
        cutoff = pd.to_datetime(curr_date)
        df = df[df["报告日"] <= cutoff]

    # Filter by frequency (annual = month 12 reports only)
    if freq.lower() == "annual" and "报告日" in df.columns:
        months = pd.to_datetime(df["报告日"], errors="coerce").dt.month
        df = df[months == 12]

    return df.head(8)


def get_balance_sheet(
    ticker: Annotated[str, "A-stock code"],
    freq: Annotated[str, "frequency: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    """Get balance sheet via Sina direct HTTP API."""
    code = _normalize_ticker(ticker)

    try:
        df = _get_financial_report_sina(code, "资产负债表", freq, curr_date)

        if df.empty:
            return f"No balance sheet data found for A-stock '{code}'"

        csv_string = df.to_csv(index=False)

        header = f"# Balance Sheet for {code} (A-stock, {freq})\n"
        header += "# Data source: sina direct HTTP\n"
        header += (
            f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        )

        return header + csv_string

    except Exception as e:
        return f"Error retrieving balance sheet for {code}: {str(e)}"


# ---- 5. get_cashflow ----


def get_cashflow(
    ticker: Annotated[str, "A-stock code"],
    freq: Annotated[str, "frequency: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    """Get cash flow statement via Sina direct HTTP API."""
    code = _normalize_ticker(ticker)

    try:
        df = _get_financial_report_sina(code, "现金流量表", freq, curr_date)

        if df.empty:
            return f"No cash flow data found for A-stock '{code}'"

        csv_string = df.to_csv(index=False)

        header = f"# Cash Flow for {code} (A-stock, {freq})\n"
        header += "# Data source: sina direct HTTP\n"
        header += (
            f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        )

        return header + csv_string

    except Exception as e:
        return f"Error retrieving cash flow for {code}: {str(e)}"


# ---- 6. get_income_statement ----


def get_income_statement(
    ticker: Annotated[str, "A-stock code"],
    freq: Annotated[str, "frequency: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    """Get income statement via Sina direct HTTP API."""
    code = _normalize_ticker(ticker)

    try:
        df = _get_financial_report_sina(code, "利润表", freq, curr_date)

        if df.empty:
            return f"No income statement data found for A-stock '{code}'"

        csv_string = df.to_csv(index=False)

        header = f"# Income Statement for {code} (A-stock, {freq})\n"
        header += "# Data source: sina direct HTTP\n"
        header += (
            f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        )

        return header + csv_string

    except Exception as e:
        return f"Error retrieving income statement for {code}: {str(e)}"


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
        "cb": "callback",
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

    resp = _requests.get(url, params=params, headers=headers, timeout=15)
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

    resp = _requests.get(url, headers=headers, timeout=15)
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
        return f"No news found for A-stock '{code}'"

    news_str = ""
    count = 0
    for art in articles:
        pub_time = art.get("time", "")
        try:
            pub_dt = datetime.strptime(pub_time[:10], "%Y-%m-%d")
            if pub_dt < start_dt or pub_dt > end_dt:
                continue
        except (ValueError, IndexError):
            pass

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
        r_cls = _requests.get(cls_url, params=cls_params, headers=cls_headers, timeout=10)
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
        r_em = _requests.get(em_url, params=em_params, headers=em_headers, timeout=10)
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
        return f"No global news found for {curr_date}"

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


# ---- 9. get_insider_transactions ----


def get_insider_transactions(
    ticker: Annotated[str, "A-stock code"],
) -> str:
    """Get shareholder/insider activity via mootdx F10.

    Note: A-stock insider transaction data differs from US markets.
    Uses mootdx F10 shareholder research as the closest equivalent.
    """
    code = _normalize_ticker(ticker)

    try:
        client = _get_mootdx_client()
        text = client.F10(symbol=code, name="股东研究")

        if not text or not text.strip():
            return f"No insider/shareholder data found for A-stock '{code}'"

        header = f"# Shareholder Research for {code} (A-stock)\n"
        header += "# Note: A-stock equivalent of insider transactions\n"
        header += "# Data source: mootdx F10\n"
        header += (
            f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        )

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
                    f"{len(text) - sec4_pos - cut_at} chars truncated ...)"
                )
            text = before_sec4 + sec4_text

        return header + text

    except Exception as e:
        return f"Error retrieving insider/shareholder data for {code}: {str(e)}"


# ---- 10. get_profit_forecast ----


def get_profit_forecast(
    ticker: Annotated[str, "A-stock code"],
    curr_date: Annotated[str, "current date (unused, for interface compat)"] = None,
) -> str:
    """Get consensus EPS forecasts with forward valuation (同花顺 direct HTTP)."""
    code = _normalize_ticker(ticker)

    try:
        df = _ths_eps_forecast(code)

        if df is None or df.empty:
            return f"No analyst coverage found for A-stock '{code}'"

        lines = [
            f"# Consensus EPS Forecast for {code} (A-stock)",
            f"# Source: 同花顺 analyst consensus (direct HTTP)",
            f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]

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

        # Forward valuation
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
                    lines.append(
                        f"Forward PE (FY{years_sorted[0]}): {fwd_pe:.1f}x"
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
                                f"PEG: {peg:.2f} (CAGR={cagr * 100:.0f}%)"
                            )
                            if fwd_pe > 30:
                                digest = math.log(fwd_pe / 30) / math.log(
                                    1 + cagr
                                )
                                lines.append(
                                    f"PE Digestion to 30x: {digest:.1f} years"
                                )
                        else:
                            lines.append(
                                f"EPS declining ({cagr * 100:.0f}%), "
                                f"PEG not applicable"
                            )
        except Exception as e:
            logger.warning("Forward PE calc failed for %s: %s", code, e)

        return "\n".join(lines)

    except Exception as e:
        return f"Error retrieving profit forecast for {code}: {str(e)}"


# ---- 11. get_hot_stocks ----


def get_hot_stocks(
    curr_date: Annotated[str, "Date YYYY-MM-DD, empty string for today"] = "",
) -> str:
    """Get strong stocks with topic attribution from 同花顺 editorial team.

    Returns stocks that hit limit-up with human-curated reason tags
    explaining WHY they surged (e.g. '算力租赁+AI政务').
    """
    import requests

    if not curr_date or curr_date.strip() == "":
        curr_date = datetime.now().strftime("%Y-%m-%d")

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
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()

        if data.get("errocode", 0) != 0:
            return f"同花顺 API error: {data.get('errormsg', 'unknown')}"

        rows = data.get("data") or []
        if not rows:
            return (
                f"No hot stocks data for {curr_date} "
                f"(may be non-trading day or data not yet available)"
            )

        lines = [
            f"# Hot Stocks with Topic Attribution ({curr_date})",
            f"# Source: 同花顺 editorial (human-curated reason tags)",
            f"# Total: {len(rows)} stocks",
            "",
        ]

        from collections import Counter

        all_tags: list[str] = []

        for row in rows:
            code = row.get("code", "")
            name = row.get("name", "")
            reason = row.get("reason", "")
            zhangfu = row.get("zhangfu", "")
            huanshou = row.get("huanshou", "")
            chengjiaoe = row.get("chengjiaoe", "")
            dde = row.get("ddejingliang", "")

            lines.append(
                f"{code} {name}: +{zhangfu}% "
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

        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching hot stocks for {curr_date}: {str(e)}"


# ---- 12. get_northbound_flow ----


def _northbound_cache_path() -> str:
    """Path to local CSV cache for northbound daily close snapshots."""
    from .config import get_config

    config = get_config()
    cache_dir = config.get(
        "data_cache_dir", os.path.expanduser("~/.tradingagents/cache")
    )
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, "northbound_daily.csv")


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
    """Get northbound capital flow (沪深股通) from 同花顺 hsgtApi.

    Realtime: minute-level cumulative net buying for HGT(沪股通) + SGT(深股通).
    History: self-cached daily close snapshots (upstream APIs stopped updating
    northbound history since 2024-08).
    """
    import requests

    hsgt_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "Chrome/117.0.0.0 Safari/537.36"
        ),
        "Host": "data.hexin.cn",
        "Referer": "https://data.hexin.cn/",
    }

    lines = [
        f"# Northbound Capital Flow ({curr_date})",
        "# Source: 同花顺 hsgtApi (沪深股通) + local cache",
        "",
    ]

    hgt_close = 0.0
    sgt_close = 0.0
    got_realtime = False

    try:
        url_rt = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
        r = requests.get(url_rt, headers=hsgt_headers, timeout=10)
        d = r.json()

        times = d.get("time", [])
        hgt = d.get("hgt", [])
        sgt = d.get("sgt", [])

        if times:
            lines.append("## Realtime (cumulative net buying, 亿元)")
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
            lines.append("No realtime data (non-trading hours or holiday)")

        if got_realtime:
            today_str = datetime.now().strftime("%Y-%m-%d")
            _save_northbound_snapshot(today_str, hgt_close, sgt_close)

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

        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching northbound flow: {str(e)}"


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
    """Get concept/sector/region blocks that a stock belongs to (百度股市通).

    Returns industry classification (申万), concept themes, and region.
    Each block includes current day's change percentage.
    """
    import requests

    code = _normalize_ticker(ticker)

    try:
        url = (
            "https://finance.pae.baidu.com/api/getrelatedblock"
            f'?stock=[{{"code":"{code}","market":"ab","type":"stock"}}]'
            "&finClientType=pc"
        )
        r = requests.get(url, headers=_BAIDU_PAE_HEADERS, timeout=10)
        d = r.json()

        if str(d.get("ResultCode", -1)) != "0":
            return (
                f"Baidu PAE error: ResultCode={d.get('ResultCode')} "
                f"{d.get('ResultMsg', '')}"
            )

        result = d.get("Result", {})
        categories = result.get(code, [])
        if not categories:
            return f"No concept/block data for {code}"

        lines = [
            f"# Concept & Sector Blocks for {code} (A-stock)",
            f"# Source: 百度股市通 (Baidu PAE)",
            f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]

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

        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching concept blocks for {code}: {str(e)}"


# ---- 14. get_fund_flow ----


def get_fund_flow(
    ticker: Annotated[str, "A-stock code"],
    curr_date: Annotated[str, "Date YYYY-MM-DD"],
    include_history: Annotated[
        bool, "Include historical daily fund flow (last 20 days)"
    ] = True,
) -> str:
    """Get individual stock fund flow from 东财 push2.

    Realtime: minute-level main/large/medium/small/super order net inflow.
    History: daily net inflow for 20 trading days (push2his).

    V0.2.7: replaced 百度 PAE (fundflow/fundsortlist, offline since 2026-05)
    with 东财 push2 fund flow API.
    """
    import requests as _req

    code = _normalize_ticker(ticker)
    secid = f"1.{code}" if code.startswith("6") else f"0.{code}"
    lines = [
        f"# Fund Flow for {code} (A-stock)",
        f"# Source: 东财 push2 (Eastmoney)",
        f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    try:
        # Realtime minute-level fund flow
        url_rt = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
        params_rt = {
            "secid": secid, "klt": 1,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
        }
        r = _req.get(url_rt, params=params_rt, timeout=10)
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
        else:
            lines.append(
                "No realtime fund flow (non-trading hours or holiday)"
            )

        # Historical daily fund flow (push2his)
        if include_history:
            url_hist = (
                "https://push2his.eastmoney.com"
                "/api/qt/stock/fflow/daykline/get"
            )
            params_hist = {
                "secid": secid, "lmt": 20, "klt": 101,
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57",
            }
            rh = _req.get(
                url_hist, params=params_hist, timeout=10
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

        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching fund flow for {code}: {str(e)}"


# ---------------------------------------------------------------------------
# 15. Dragon Tiger Board (龙虎榜)
# ---------------------------------------------------------------------------

def get_dragon_tiger_board(
    ticker: str,
    trade_date: str,
    look_back_days: int = 30,
) -> str:
    """Get dragon-tiger board (龙虎榜) appearances and seat details.

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

    # 1. 上榜记录 — eastmoney datacenter direct HTTP
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

    # 3. 机构动向 — 从买卖席位明细筛选机构专用席位 (OPERATEDEPT_CODE="0")
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
        url = "https://push2.eastmoney.com/api/qt/clist/get"
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
        r = _requests.get(url, params=params, headers={"User-Agent": _UA}, timeout=15)
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

    # 尝试从本地缓存加载
    cache_file = _daily_cache_path(code, level)

    if os.path.exists(cache_file):
        df = pd.read_csv(cache_file, on_bad_lines="skip", encoding="utf-8")
        df["Date"] = pd.to_datetime(df["Date"])
        cutoff = pd.to_datetime(curr_date)
        df = df[df["Date"] <= cutoff]
        if start_date:
            df = df[df["Date"] >= pd.to_datetime(start_date)]
    else:
        # 没有缓存，尝试在线获取
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
