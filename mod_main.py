"""
SOOP KBO HLS Proxy Plugin
=========================
SOOP KBO 스트림을 HLS 프록시로 제공합니다.
streamlink 없이 SOOP API를 직접 호출합니다.

[ 엔드포인트 ]
  /soop_kbo/playlist.m3u8          - Plex / IPTV 플레이리스트
  /soop_kbo/channel/<id>.m3u8      - 채널 HLS 프록시
  /soop_kbo/sub                    - 서브 플레이리스트 프록시
  /soop_kbo/seg                    - 세그먼트 프록시
  /soop_kbo/cache/clear            - 스트림 캐시 초기화

[ 설정 ]
  proxy_url  : http://user:pass@host:port (필수)
  channel_urls : JSON (채널 ID → URL 매핑)
"""
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from base64 import urlsafe_b64decode, urlsafe_b64encode
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from flask import Response, abort, jsonify, render_template, request
from plugin import F, PluginModuleBase  # type: ignore # pylint: disable=import-error

from .setup import P

logger = P.logger
package_name = P.package_name
ModelSetting = P.ModelSetting
blueprint = P.blueprint
SystemModelSetting = F.SystemModelSetting
scheduler = F.scheduler
PLUGIN_VERSION = "unknown"


def _load_plugin_version() -> str:
    """info.yaml에서 버전을 읽어 UI/로그에 노출."""
    try:
        info_path = os.path.join(os.path.dirname(__file__), "info.yaml")
        with open(info_path, encoding="utf-8") as fp:
            text = fp.read()
        m = re.search(r'^\s*version\s*:\s*"([^"]+)"\s*$', text, flags=re.MULTILINE)
        if m:
            return m.group(1).strip()
    except Exception:
        logger.exception("[SOOP_KBO] info.yaml 버전 로드 실패")
    return "unknown"


PLUGIN_VERSION = _load_plugin_version()

# ─── 채널 기본 목록 ───────────────────────────────────────────────────────────
DEFAULT_CHANNEL_URLS = {
    "kboglobal1": "https://play.sooplive.co.kr/kboglobal1",
    "kboglobal2": "https://play.sooplive.co.kr/kboglobal2",
    "kboglobal3": "https://play.sooplive.co.kr/kboglobal3",
    "kboglobal4": "https://play.sooplive.co.kr/kboglobal4",
    "kboglobal5": "https://play.sooplive.co.kr/kboglobal5",
}

CHANNEL_NAMES = {
    "kboglobal1": "SOOP KBO1",
    "kboglobal2": "SOOP KBO2",
    "kboglobal3": "SOOP KBO3",
    "kboglobal4": "SOOP KBO4",
    "kboglobal5": "SOOP KBO5",
}

_TEAM_KO = {
    "LOTTE": "롯데", "DOOSAN": "두산", "KIWOOM": "키움",
    "KIA": "기아", "SAMSUNG": "삼성", "HANWHA": "한화",
}


def _localize_title(title: str) -> str:
    """경기명의 영문 팀명을 한글로 치환."""
    for en, ko in _TEAM_KO.items():
        title = title.replace(en, ko)
    return title


def _load_channel_urls() -> dict:
    """DB에서 채널 URL을 로드. 없으면 기본값 반환."""
    try:
        raw = ModelSetting.get("channel_urls") or ""
        if raw.strip():
            data = json.loads(raw)
            if isinstance(data, dict) and data:
                return data
    except Exception:
        logger.exception("[SOOP_KBO] channel_urls 파싱 실패, 기본값 사용")
    return dict(DEFAULT_CHANNEL_URLS)


def _channel_list() -> list[dict]:
    """채널 목록 반환 (id, name, url)."""
    urls = _load_channel_urls()
    result = []
    for ch_id, url in urls.items():
        result.append({
            "id": ch_id,
            "name": CHANNEL_NAMES.get(ch_id, ch_id),
            "url": url,
        })
    def sort_key(item: dict) -> tuple[int, str]:
        m = re.search(r"(\d+)$", item.get("id", ""))
        if m:
            return (int(m.group(1)), item.get("id", ""))
        return (9999, item.get("id", ""))

    result.sort(key=sort_key)
    return result


# ─── 스트림 캐시 ──────────────────────────────────────────────────────────────
_cache: dict = {}
_cache_lock = threading.Lock()
CACHE_TTL = 1800  # 30분
TITLE_CACHE_TTL = 60  # 1분
QUALITY_LOG_INTERVAL = 30
CDN_TYPE_MAPPING = {
    "gs_cdn": "gs_cdn_pc_web",
    "lg_cdn": "lg_cdn_pc_web",
}
CHANNEL_API_URL = "https://live.sooplive.com/afreeca/player_live_api.php"
_http_lock = threading.Lock()
_http_shared_session: requests.Session | None = None
_http_shared_proxy: str | None = None
_quality_state_lock = threading.Lock()
_channel_quality: dict[str, str] = {}
_quality_log_last_ts: dict[str, float] = {}
_title_cache_lock = threading.Lock()
_title_cache: dict[str, tuple[dict, float]] = {}


# ─── 설정 헬퍼 ───────────────────────────────────────────────────────────────
def _get_proxy_url() -> str:
    """설정된 프록시 URL 반환. (미설정 시 빈 문자열로 직접 접속 허용)"""
    return (ModelSetting.get("proxy_url") or "").strip()


def _build_http_session(proxy_url: str) -> requests.Session:
    sess = requests.Session()
    sess.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    if proxy_url:
        sess.proxies.update({"http": proxy_url, "https": proxy_url})
    return sess


def _http_session() -> requests.Session:
    """프록시 세션 재사용으로 매 세그먼트 TLS 핸드셰이크 비용 감소."""
    global _http_shared_session, _http_shared_proxy
    purl = _get_proxy_url()
    with _http_lock:
        if _http_shared_session is None or _http_shared_proxy != purl:
            _http_shared_session = _build_http_session(purl)
            _http_shared_proxy = purl
        return _http_shared_session


# ─── SOOP API 직접 호출 ───────────────────────────────────────────────────────
def _parse_bj_id(page_url: str) -> str:
    """https://play.sooplive.co.kr/<bjid> 또는 www.sooplive.co.kr/<bjid> 에서 BJ ID 추출."""
    path = urlparse(page_url).path.strip("/")
    # path 예: "kboglobal1" 또는 "kboglobal1/1" → 첫 번째 세그먼트
    return path.split("/")[0]


def _extract_bno_from_page(sess: requests.Session, page_url: str) -> str | None:
    """채널 페이지에서 방송번호(nBroadNo) 추출."""
    try:
        resp = sess.get(page_url, timeout=10)
        resp.raise_for_status()
        m = re.search(r"window\.nBroadNo\s*=\s*(\d+)\s*;", resp.text)
        if m:
            return m.group(1)
    except Exception:
        logger.exception("[SOOP_KBO] nBroadNo 추출 실패")
    return None


def _looks_like_m3u8_url(url: str) -> bool:
    """URL 문자열만으로 m3u8 가능성을 1차 판별."""
    u = url.lower()
    return ".m3u8" in u or "/hls/" in u or "/live/" in u


def _probe_m3u8_url(sess: requests.Session, url: str) -> bool:
    """실제 요청으로 m3u8 여부를 확인."""
    try:
        resp = sess.get(url, timeout=10)
        if resp.status_code != 200:
            return False
        body_head = (resp.text or "")[:256]
        return "#EXTM3U" in body_head or "#EXT-X-" in body_head
    except Exception:
        return False


def _normalize_return_type(cdn: str) -> str:
    """SOOP CDN 타입을 broad_stream_assign용 타입으로 정규화."""
    for key, mapped in CDN_TYPE_MAPPING.items():
        if key in (cdn or ""):
            return mapped
    return cdn or "gs_cdn_pc_web"


def _get_aid(sess: requests.Session, bj_id: str, bno: str, quality: str) -> str | None:
    """type=aid API로 aid 토큰 획득."""
    payload = {
        "bid": bj_id,
        "bno": bno,
        "type": "aid",
        "quality": quality,
        "mode": "landing",
        "player_type": "html5",
        "stream_type": "common",
        "from_api": "0",
        "pwd": "",
    }
    resp = sess.post(CHANNEL_API_URL, data=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        return None
    channel = data.get("CHANNEL", {}) if isinstance(data.get("CHANNEL"), dict) else {}
    result = channel.get("RESULT")
    if result in (1, "1"):
        return channel.get("AID")
    return None


def _get_view_url(sess: requests.Session, rmd: str, cdn: str, bno: str, quality: str) -> str | None:
    """broad_stream_assign.html 호출로 m3u8 view_url 획득."""
    rmd_base = rmd if rmd.startswith("http") else f"https://{rmd}"
    rmd_base = rmd_base.rstrip("/")
    params = {
        "return_type": _normalize_return_type(cdn),
        "broad_key": f"{bno}-common-{quality}-hls",
    }
    resp = sess.get(f"{rmd_base}/broad_stream_assign.html", params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        return data.get("view_url")
    return None


def _get_hls_url_from_api(page_url: str, sess: requests.Session) -> str:
    """SOOP 방송국 API에서 HLS URL 획득."""
    bj_id = _parse_bj_id(page_url)
    logger.info("[SOOP_KBO] BJ ID: %s / page_url: %s", bj_id, page_url)

    # 1) 라이브 정보 API
    api_url = CHANNEL_API_URL
    payload = {
        "bid": bj_id,
        "type": "live",
        "quality": "original",
        "player_type": "html5",
        "mode": "landing",
        "from_api": "0",
        "pwd": "",
        "stream_type": "common",
        "is_revive": "false",
    }
    headers = {
        "Referer": page_url,
        "Origin": "https://play.sooplive.com",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    sess.headers.update({"Referer": page_url, "Origin": "https://play.sooplive.com"})

    def request_live(extra: dict | None = None) -> dict:
        req_payload = dict(payload)
        if extra:
            req_payload.update(extra)
        r = sess.post(api_url, data=req_payload, headers=headers, timeout=15)
        r.raise_for_status()
        return r.json()

    data = request_live()
    logger.info("[SOOP_KBO] API 응답 keys: %s", list(data.keys()) if isinstance(data, dict) else type(data))

    if not isinstance(data, dict):
        raise RuntimeError(f"SOOP API 응답 형식 오류: {type(data)}")

    channel = data.get("CHANNEL", {})

    # 응답 스키마가 변형되는 경우를 대비해 여러 키를 순차적으로 확인
    # - data.result / data.RESULT
    # - data.CHANNEL.result / data.CHANNEL.RESULT
    result_code = (
        data.get("result")
        if data.get("result") is not None
        else data.get("RESULT")
    )
    if result_code is None and isinstance(channel, dict):
        result_code = (
            channel.get("result")
            if channel.get("result") is not None
            else channel.get("RESULT")
        )
    if result_code is None:
        result_code = 0

    logger.info("[SOOP_KBO] API 응답 result=%s channel_keys=%s", result_code, list(channel.keys()) if isinstance(channel, dict) else [])

    # 일부 채널은 bno 없으면 result=0이 내려와서 페이지의 nBroadNo로 재시도
    if result_code != 1:
        bno_hint = _extract_bno_from_page(sess, page_url)
        if bno_hint:
            logger.info("[SOOP_KBO] bno 재시도: %s", bno_hint)
            data = request_live({"bno": bno_hint})
            channel = data.get("CHANNEL", {}) if isinstance(data.get("CHANNEL"), dict) else {}
            result_code = channel.get("RESULT")
            if result_code is None:
                result_code = data.get("result") or data.get("RESULT") or 0
            logger.info("[SOOP_KBO] bno 재시도 result=%s", result_code)

    if result_code != 1:
        message = data.get("message") or data.get("MSG")
        if isinstance(channel, dict):
            message = message or channel.get("RESULT_MSG") or channel.get("ERROR")
        raise RuntimeError(
            f"SOOP API result={result_code}: 방송 중이 아니거나 오류 ({bj_id}) "
            f"message={message}"
        )

    hls_url = channel.get("RMD") or channel.get("CDN") or ""
    if not hls_url:
        # CDN 목록에서 직접 구성
        cdn_list = channel.get("CDN_LIST", [])
        logger.info("[SOOP_KBO] CDN_LIST: %s", cdn_list)
        raise RuntimeError(f"HLS URL을 찾을 수 없음. CHANNEL keys: {list(channel.keys())}")

    bno = str(channel.get("BNO", "")).strip()
    cdn = str(channel.get("CDN", "")).strip()
    viewpreset = channel.get("VIEWPRESET") or []

    # 1) 이미 완전한 m3u8 URL이면 그대로 사용
    if hls_url.startswith("http") and _looks_like_m3u8_url(hls_url):
        with _quality_state_lock:
            _channel_quality[bj_id] = "direct"
        return hls_url

    # 2) streamlink soop 플러그인 방식: type=aid + broad_stream_assign
    # viewpreset 품질 우선순위: 설정값 우선, 없으면 hd > original > sd
    qualities = []
    if isinstance(viewpreset, list):
        for item in viewpreset:
            if isinstance(item, dict):
                q = item.get("name")
                if q and q != "auto":
                    qualities.append(q)
    pref_raw = (ModelSetting.get("quality_preference") or "original,hd,sd").strip()
    pref = [x.strip() for x in pref_raw.split(",") if x.strip()]
    if qualities:
        ordered = [q for q in pref if q in qualities]
        remains = [q for q in qualities if q not in ordered]
        qualities = ordered + remains
    else:
        qualities = pref if pref else ["original", "hd", "sd"]

    if bno and hls_url:
        for quality in qualities:
            try:
                aid = _get_aid(sess, bj_id, bno, quality)
                if not aid:
                    continue
                view_url = _get_view_url(sess, hls_url, cdn, bno, quality)
                if not view_url:
                    continue
                final_url = f"{view_url}{'&' if '?' in view_url else '?'}aid={aid}"
                if _probe_m3u8_url(sess, final_url):
                    with _quality_state_lock:
                        _channel_quality[bj_id] = quality
                    return final_url
            except Exception:
                logger.exception("[SOOP_KBO] stream_assign 실패 quality=%s", quality)

    # 3) 마지막 fallback
    host = hls_url if hls_url.startswith("http") else f"https://{hls_url}"
    logger.warning("[SOOP_KBO] stream_assign 실패, 원본 URL fallback: %.120s", host)
    hls_url = host

    logger.info("[SOOP_KBO] HLS URL: %.120s", hls_url)
    return hls_url


# ─── 스트림 관리 ─────────────────────────────────────────────────────────────
def _get_hls_url(channel_id: str) -> str:
    """캐시에서 HLS URL을 가져오거나 SOOP API로 새로 획득."""
    with _cache_lock:
        if channel_id in _cache:
            hls_url, cached_at = _cache[channel_id]
            if time.time() - cached_at < CACHE_TTL:
                return hls_url
            del _cache[channel_id]

    urls = _load_channel_urls()
    if channel_id not in urls:
        raise KeyError(f"알 수 없는 채널: {channel_id}")
    page_url = urls[channel_id]

    sess = _http_session()
    hls_url = _get_hls_url_from_api(page_url, sess)

    with _cache_lock:
        _cache[channel_id] = (hls_url, time.time())
    return hls_url


def _extract_result_code(data: dict, channel: dict) -> int:
    result_code = data.get("result", data.get("RESULT"))
    if result_code is None:
        result_code = channel.get("result", channel.get("RESULT"))
    if result_code is None:
        result_code = 0
    try:
        return int(result_code)
    except Exception:
        return 0


def _fetch_channel_live_meta(page_url: str, sess: requests.Session) -> dict:
    """채널 페이지 URL에서 라이브 메타(제목/온에어)를 조회."""
    bj_id = _parse_bj_id(page_url)
    payload = {
        "bid": bj_id,
        "type": "live",
        "quality": "original",
        "player_type": "html5",
        "mode": "landing",
        "from_api": "0",
        "pwd": "",
        "stream_type": "common",
        "is_revive": "false",
    }
    headers = {
        "Referer": page_url,
        "Origin": "https://play.sooplive.com",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    def request_live(extra: dict | None = None) -> dict:
        req_payload = dict(payload)
        if extra:
            req_payload.update(extra)
        resp = sess.post(CHANNEL_API_URL, data=req_payload, headers=headers, timeout=5)
        resp.raise_for_status()
        return resp.json()

    data = request_live()
    channel = data.get("CHANNEL", {}) if isinstance(data.get("CHANNEL"), dict) else {}
    result_code = _extract_result_code(data, channel)

    # 일부 채널은 bno 포함 재시도 필요
    if result_code != 1:
        if bno_hint := _extract_bno_from_page(sess, page_url):
            data = request_live({"bno": bno_hint})
            channel = data.get("CHANNEL", {}) if isinstance(data.get("CHANNEL"), dict) else {}
            result_code = _extract_result_code(data, channel)

    title = ""
    if isinstance(channel, dict):
        title = (channel.get("TITLE") or "").strip()
    return {
        "onair": result_code == 1,
        "title": title,
        "bj_id": bj_id,
    }


def _fetch_title_from_page(page_url: str, sess: requests.Session) -> str:
    """페이지 HTML에서 제목 fallback 추출 (//*[@id='infoTitle'] 포함)."""
    try:
        resp = sess.get(page_url, timeout=5)
        resp.raise_for_status()
        text = resp.text
        # 1) 요청하신 id=infoTitle 영역
        m = re.search(r'id=["\']infoTitle["\'][^>]*>(.*?)<', text, flags=re.IGNORECASE | re.DOTALL)
        if m:
            title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            if title:
                return title
        # 2) JS 변수
        m = re.search(r'window\.szBroadTitle\s*=\s*["\']([^"\']+)["\']', text)
        if m:
            return m.group(1).strip()
        # 3) og:title
        m = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', text, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip()
    except Exception:
        logger.exception("[SOOP_KBO] 페이지 제목 fallback 실패: %s", page_url)
    return ""


def _get_channel_live_meta_cached(channel_id: str, page_url: str, sess: requests.Session) -> dict:
    now = time.time()
    with _title_cache_lock:
        if channel_id in _title_cache:
            item, ts = _title_cache[channel_id]
            if now - ts < TITLE_CACHE_TTL:
                return item
    try:
        item = _fetch_channel_live_meta(page_url, sess)
        if not item.get("title"):
            item["title"] = _fetch_title_from_page(page_url, sess)
    except Exception:
        logger.exception("[SOOP_KBO] 경기정보 조회 실패: %s", channel_id)
        item = {"onair": False, "title": _fetch_title_from_page(page_url, sess), "bj_id": _parse_bj_id(page_url)}
    with _title_cache_lock:
        _title_cache[channel_id] = (item, now)
    return item


def _refresh_channel_titles(log_prefix: str = "WEB") -> tuple[list[dict], str]:
    """채널 제목을 병렬 갱신하고 rows와 요약 문자열을 반환."""
    started_at = time.time()
    channels = _channel_list()
    proxy_url = _get_proxy_url()
    rows = []
    logger.info("[SOOP_KBO][%s] channel_list start channels=%d", log_prefix, len(channels))

    def build_row(ch: dict) -> dict:
        t0 = time.time()
        # requests.Session은 thread-safe 보장이 없어 스레드별 세션 사용
        sess = _build_http_session(proxy_url)
        meta = _get_channel_live_meta_cached(ch["id"], ch["url"], sess)
        title = (meta.get("title") or "").strip() if isinstance(meta.get("title"), str) else ""
        if not title or "오프라인" in title:
            title = "경기 대기중입니다"
        else:
            title = _localize_title(title)
        elapsed = round((time.time() - t0) * 1000)
        logger.info(
            "[SOOP_KBO][%s] channel_list item id=%s onair=%s title=%s elapsed_ms=%s",
            log_prefix,
            ch["id"],
            meta.get("onair"),
            (title[:80] if isinstance(title, str) else title),
            elapsed,
        )
        return {
            "source": "soop_kbo",
            "channel_id": ch["id"],
            "name": ch["name"],
            "program": {"title": title},
        }

    with ThreadPoolExecutor(max_workers=min(5, len(channels) or 1)) as exe:
        fut_map = {exe.submit(build_row, ch): ch for ch in channels}
        for fut in as_completed(fut_map):
            ch = fut_map[fut]
            try:
                rows.append(fut.result())
            except Exception:
                logger.exception("[SOOP_KBO][%s] channel_list item 실패: %s", log_prefix, ch["id"])
                rows.append(
                    {
                        "source": "soop_kbo",
                        "channel_id": ch["id"],
                        "name": ch["name"],
                        "program": {"title": "경기 대기중입니다"},
                    }
                )

    order = {ch["id"]: idx for idx, ch in enumerate(channels)}
    rows.sort(key=lambda x: order.get(x.get("channel_id"), 999))
    summary = f"elapsed_ms={int((time.time() - started_at) * 1000)} rows={len(rows)}"
    logger.info("[SOOP_KBO][%s] channel_list done %s", log_prefix, summary)
    return rows, summary


def _rows_from_title_cache() -> list[dict]:
    """네트워크 조회 없이 현재 캐시된 제목만으로 목록 구성."""
    channels = _channel_list()
    rows = []
    with _title_cache_lock:
        for ch in channels:
            cached = _title_cache.get(ch["id"])
            meta = cached[0] if cached else {}
            title = meta.get("title") if isinstance(meta, dict) else ""
            if not title:
                title = "LIVE"
            rows.append(
                {
                    "source": "soop_kbo",
                    "channel_id": ch["id"],
                    "name": ch["name"],
                    "program": {"title": title},
                }
            )
    return rows


def _fallback_rows_waiting() -> list[dict]:
    """API 조회 실패 시 UI에 항상 표시할 기본 rows."""
    return [
        {
            "source": "soop_kbo",
            "channel_id": ch["id"],
            "name": ch["name"],
            "program": {"title": "대기중"},
        }
        for ch in _channel_list()
    ]


def _trigger_plex_refresh() -> tuple[bool, str]:
    """plex_mate 경유 또는 Plex API 직접 호출로 섹션 메타데이터 새로 고침."""
    try:
        section_id = int((ModelSetting.get("plex_section_id") or "0").strip())
    except Exception:
        return False, "invalid plex_section_id"
    if section_id <= 0:
        return False, "plex_section_id 미설정"

    # 1) plex_mate PlexWebHandle 경유
    try:
        plugin = F.PluginManager.get_plugin_instance("plex_mate")
        if plugin is not None and hasattr(plugin, "PlexWebHandle"):
            handle = plugin.PlexWebHandle
            for method_name in ("section_refresh", "section_refresh_metadata", "refresh_section"):
                method = getattr(handle, method_name, None)
                if callable(method):
                    method(section_id)
                    return True, f"plex_mate {method_name} ok: section={section_id}"
    except Exception:
        logger.exception("[SOOP_KBO] plex_mate refresh 실패")

    # 2) Plex API 직접 호출 (plex_mate base_url/base_token 사용)
    plex_url = plex_token = ""
    try:
        plugin = F.PluginManager.get_plugin_instance("plex_mate")
        if plugin is not None and hasattr(plugin, "ModelSetting"):
            plex_url = (plugin.ModelSetting.get("base_url") or "").strip().rstrip("/")
            plex_token = (plugin.ModelSetting.get("base_token") or "").strip()
    except Exception:
        logger.exception("[SOOP_KBO] plex_mate 설정 조회 실패")

    if not plex_url or not plex_token:
        return False, "plex_mate base_url/base_token 미설정"

    try:
        resp = requests.get(
            f"{plex_url}/library/sections/{section_id}/refresh",
            params={"X-Plex-Token": plex_token, "force": "1"},
            timeout=15,
        )
        if str(resp.status_code).startswith("2"):
            return True, f"Plex API refresh ok: section={section_id}"
        return False, f"Plex API status={resp.status_code}"
    except Exception as e:
        return False, f"Plex refresh 예외: {e}"


def _update_alive_yaml() -> tuple[bool, str]:
    """alive.yaml의 kboglobal1~5 name 필드를 채널 캐시 경기명으로 업데이트. 없으면 fix_url에 추가."""
    enable = (ModelSetting.get("alive_update_enable") or "False").strip().lower() in ("true", "1", "on", "yes")
    if not enable:
        return False, "alive 연동 비활성화"

    alive_path = (ModelSetting.get("alive_yaml_path") or "").strip()
    if not alive_path:
        logger.info("[SOOP_KBO] alive_yaml_path 미설정 - alive.yaml 업데이트 건너뜀")
        return False, "alive_yaml_path 미설정"

    path = Path(alive_path)
    if not path.exists():
        logger.info("[SOOP_KBO] alive.yaml 파일 없음: %s", alive_path)
        return False, f"파일 없음: {alive_path}"

    title_map: dict[str, str] = {}
    try:
        raw = ModelSetting.get("channel_list_cache") or ""
        if raw:
            for row in json.loads(raw):
                ch_id = row.get("channel_id", "")
                title = row.get("program", {}).get("title", "")
                if ch_id and title and "대기중" not in title:
                    title_map[ch_id] = title
    except Exception:
        logger.exception("[SOOP_KBO] alive title_map 구성 실패")

    default_names = {f"kboglobal{i}": f"[SOOP] KBO CH.{i} (대기중)" for i in range(1, 6)}
    target_channels = list(default_names.keys())

    content = path.read_text(encoding="utf-8")
    new_lines = list(content.splitlines(keepends=True))

    # ── Phase 1: 기존 name 필드 업데이트 ──────────────────────────────────────
    current_channel: str | None = None
    channel_indent = 0
    found_channels: set[str] = set()
    updated = 0

    for i, line in enumerate(new_lines):
        rstripped = line.rstrip('\r\n')
        eol = line[len(rstripped):]

        m = re.match(r'^(\s+)(kboglobal[1-5]):\s*$', rstripped)
        if m:
            current_channel = m.group(2)
            channel_indent = len(m.group(1))
            found_channels.add(current_channel)
            continue

        if current_channel:
            ls = rstripped.lstrip()
            li = len(rstripped) - len(ls) if ls else channel_indent + 1
            if ls and li <= channel_indent:
                current_channel = None
                channel_indent = 0
            elif re.match(r'^\s+name:\s*', rstripped):
                new_name = title_map.get(current_channel, default_names[current_channel])
                indent_m = re.match(r'^(\s+)', rstripped)
                indent = indent_m.group(1) if indent_m else "      "
                escaped = new_name.replace("'", "''")
                new_line = f"{indent}name: '{escaped}'{eol}"
                if new_line != line:
                    new_lines[i] = new_line
                    updated += 1
                current_channel = None
                channel_indent = 0

    # ── Phase 2: 없는 채널 fix_url 섹션에 추가 ───────────────────────────────
    missing = [ch for ch in target_channels if ch not in found_channels]
    added = 0

    if missing:
        stream_base_url = (ModelSetting.get("stream_base_url") or "").strip().rstrip("/")
        fix_url_indent = -1
        entry_indent = -1
        last_fix_url_idx = -1
        in_fix_url = False

        for i, line in enumerate(new_lines):
            rstripped = line.rstrip('\r\n')
            stripped = rstripped.strip()
            m = re.match(r'^(\s*)fix_url:\s*$', rstripped)
            if m:
                in_fix_url = True
                fix_url_indent = len(m.group(1))
                continue
            if in_fix_url:
                if not stripped or stripped.startswith('#'):
                    continue
                li = len(rstripped) - len(rstripped.lstrip())
                if li <= fix_url_indent:
                    in_fix_url = False
                else:
                    last_fix_url_idx = i
                    if entry_indent < 0:
                        entry_indent = li

        if last_fix_url_idx >= 0:
            if entry_indent < 0:
                entry_indent = fix_url_indent + 2
            e_ind = " " * entry_indent
            p_ind = " " * (entry_indent + 2)
            insert_lines = []
            for ch_id in missing:
                new_name = title_map.get(ch_id, default_names[ch_id])
                escaped = new_name.replace("'", "''")
                url = f"{stream_base_url}/soop_kbo/channel/{ch_id}.m3u8" if stream_base_url else ""
                insert_lines.append(f"{e_ind}{ch_id}:\n")
                insert_lines.append(f"{p_ind}name: '{escaped}'\n")
                if url:
                    insert_lines.append(f"{p_ind}url: {url}\n")
            new_lines = new_lines[:last_fix_url_idx + 1] + insert_lines + new_lines[last_fix_url_idx + 1:]
            added = len(missing)
            logger.info("[SOOP_KBO] alive.yaml 새 항목 추가: %s", missing)
        else:
            logger.warning("[SOOP_KBO] alive.yaml fix_url 섹션을 찾을 수 없어 추가 건너뜀")

    new_content = "".join(new_lines)
    if new_content == content:
        logger.info("[SOOP_KBO] alive.yaml 변경 없음")
        return True, "변경 없음"

    path.write_text(new_content, encoding="utf-8")
    msg_parts = []
    if updated:
        msg_parts.append(f"업데이트 {updated}개")
    if added:
        msg_parts.append(f"추가 {added}개")
    msg = ", ".join(msg_parts) or "변경 없음"
    logger.info("[SOOP_KBO] alive.yaml %s", msg)
    return True, msg


def _write_show_yaml() -> tuple[bool, str]:
    """library_path/kbo/show.yaml 생성. 경기명 캐시가 있으면 title에 반영."""
    try:
        import yaml
    except ImportError:
        return False, "PyYAML 미설치"

    from datetime import datetime

    library_path = (ModelSetting.get("library_path") or "").strip()
    stream_base_url = (ModelSetting.get("stream_base_url") or "").strip().rstrip("/")
    plex_section_id = (ModelSetting.get("plex_section_id") or "").strip()

    missing = []
    if not library_path:
        missing.append("library_path")
    if not stream_base_url:
        missing.append("stream_base_url")
    if not plex_section_id:
        missing.append("plex_section_id")
    if missing:
        msg = f"미설정 항목: {', '.join(missing)}"
        logger.info("[SOOP_KBO] show.yaml 건너뜀 - %s", msg)
        return False, msg


    # 캐시에서 채널별 경기명 가져오기 (없으면 기본값 사용)
    title_map: dict[str, str] = {}
    try:
        raw = ModelSetting.get("channel_list_cache") or ""
        if raw:
            for row in json.loads(raw):
                ch_id = row.get("channel_id", "")
                title = row.get("program", {}).get("title", "")
                if ch_id and title and "대기중" not in title:
                    title_map[ch_id] = title
    except Exception:
        logger.exception("[SOOP_KBO] show.yaml title_map 구성 실패")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    extras = []
    for i in range(1, 6):
        ch_id = f"kboglobal{i}"
        title = title_map.get(ch_id, f"SOOP KBO{i}")
        extras.append({
            "mode": "m3u8",
            "type": "featurette",
            "param": f"{stream_base_url}/soop_kbo/channel/{ch_id}.m3u8",
            "title": title,
            "thumb": f"https://raw.githubusercontent.com/zeliit/PlexLiveTV/main/thumb/soop_kbo{i}.png",
        })

    show = {
        "primary": True,
        "code": "kbo",
        "title": "숲 한국 프로야구",
        "posters": "https://raw.githubusercontent.com/zeliit/PlexLiveTV/main/poster/kbo.webp",
        "summary": f"SOOP KBO 채널\n\n  {now}",
        "extras": extras,
    }

    try:
        import shutil
        output_dir = Path(library_path) / "kbo"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "show.yaml"
        output_path.write_text(
            yaml.safe_dump(show, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        logger.info("[SOOP_KBO] show.yaml 생성: %s", output_path)

        # file/ 폴더의 파일을 라이브러리 폴더에 복사
        file_src_dir = Path(__file__).parent / "file"
        if file_src_dir.is_dir():
            for src_file in file_src_dir.iterdir():
                if src_file.is_file():
                    dst_file = output_dir / src_file.name
                    shutil.copy2(src_file, dst_file)
                    logger.info("[SOOP_KBO] 파일 복사: %s → %s", src_file.name, dst_file)

        return True, str(output_path)
    except Exception as e:
        logger.exception("[SOOP_KBO] show.yaml 생성 실패")
        return False, str(e)


# ─── M3U8 처리 ───────────────────────────────────────────────────────────────
def _b64enc(s: str) -> str:
    return urlsafe_b64encode(s.encode()).decode()


def _b64dec(s: str) -> str:
    return urlsafe_b64decode(s.encode()).decode()


def _base_url_of(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path.rsplit('/', 1)[0]}/"


def _proxy_base() -> str:
    ddns = SystemModelSetting.get("ddns").rstrip("/")
    return f"{ddns}/{package_name}"


def _rewrite_m3u8(m3u8_text: str, m3u8_url: str, channel_id: str) -> str:
    """m3u8 내 URL을 /soop_kbo/seg, /soop_kbo/sub 엔드포인트로 재작성."""
    base = _base_url_of(m3u8_url)
    pb = _proxy_base()

    def abs_url(u: str) -> str:
        u = u.strip()
        return u if u.startswith("http") else urljoin(base, u)

    def to_seg(u: str) -> str:
        return f"{pb}/seg?c={channel_id}&url={_b64enc(abs_url(u))}"

    def to_sub(u: str) -> str:
        return f"{pb}/sub?c={channel_id}&url={_b64enc(abs_url(u))}"

    result = []
    for line in m3u8_text.splitlines(keepends=True):
        s = line.rstrip("\r\n")
        if s.startswith("#"):
            s = re.sub(r'URI="([^"]+)"', lambda m: f'URI="{to_seg(m.group(1))}"', s)
            result.append(s + "\n")
        elif s:
            result.append((to_sub(s) if ".m3u8" in s else to_seg(s)) + "\n")
        else:
            result.append(line)
    return "".join(result)


# ─── ModuleMain 클래스 (플러그인 프레임워크 필수) ────────────────────────────
class ModuleMain(PluginModuleBase):
    def __init__(self, P):
        super(ModuleMain, self).__init__(P, name="main", first_menu="setting", scheduler_desc="SOOP KBO 채널목록 갱신")
        self.db_default = {
            "proxy_use": "True",
            "proxy_url": "",
            "quality_preference": "original,hd,sd",
            "channel_urls": json.dumps(DEFAULT_CHANNEL_URLS, ensure_ascii=False),
            "main_auto_start": "False",
            "main_interval": "10",
            "schedule_last_run": "",
            "schedule_last_result": "",
            "channel_list_cache": "",
            "channel_list_updated_at": "",
            "library_path": "",
            "stream_base_url": "",
            "plex_section_id": "",
            "alive_update_enable": "False",
            "alive_yaml_path": "",
        }
        # db_default는 최초 설치 시에만 동작 → 업그레이드 시 누락 키 보완
        # ModelSetting.set()은 UPDATE만 하므로 없는 키에는 raw INSERT OR IGNORE 필요
        _migration_keys = [
            ("library_path", ""),
            ("stream_base_url", ""),
            ("plex_section_id", ""),
            ("channel_list_cache", ""),
            ("channel_list_updated_at", ""),
            ("alive_update_enable", "False"),
            ("alive_yaml_path", ""),
        ]
        try:
            _app = getattr(F, 'app', None)
            if _app:
                with _app.app_context():
                    _conn = F.db.engine.raw_connection()
                    try:
                        _cur = _conn.cursor()
                        for _key, _default in _migration_keys:
                            _cur.execute(
                                f'INSERT OR IGNORE INTO {package_name}_setting ("key", value) VALUES (?, ?)',
                                (_key, _default),
                            )
                            logger.debug("[SOOP_KBO] DB키 마이그레이션: %s", _key)
                        _conn.commit()
                    finally:
                        _conn.close()
        except Exception:
            logger.exception("[SOOP_KBO] DB키 마이그레이션 실패")

    def process_menu(self, sub, _req):
        try:
            arg = P.ModelSetting.to_dict()
            arg["package_name"] = P.package_name
            arg["plugin_version"] = PLUGIN_VERSION
            arg["playlist_url"] = f"{SystemModelSetting.get('ddns')}/{P.package_name}/playlist.m3u8"
            arg["yaml_url"] = f"{SystemModelSetting.get('ddns')}/{P.package_name}/api/yaml"
            arg["default_channel_urls"] = json.dumps(DEFAULT_CHANNEL_URLS, ensure_ascii=False, indent=2)
            if sub == "setting":
                arg["is_include"] = scheduler.is_include(self.get_scheduler_name())
                arg["is_running"] = scheduler.is_running(self.get_scheduler_name())
            return render_template(f"{P.package_name}_{self.name}_{sub}.html", arg=arg)
        except Exception:
            logger.exception("메뉴 처리 중 예외:")
            return render_template("sample.html", title=f"{P.package_name} - {sub}")

    def scheduler_function(self):
        """스케줄러 실행: 채널 제목을 조회하고 DB에 저장."""
        from datetime import datetime
        try:
            rows, summary = _refresh_channel_titles("CELERY")
            fail_count = sum(1 for row in rows if row.get("program", {}).get("title") == "조회 실패")
            result_msg = f"{summary} fail={fail_count}"
            ModelSetting.set("schedule_last_result", result_msg)
            cache_json = json.dumps(rows, ensure_ascii=False)
            logger.info("[SOOP_KBO][SCHED] DB저장 시도 rows=%d json_len=%d", len(rows), len(cache_json))
            ModelSetting.set("channel_list_cache", cache_json)
            ModelSetting.set("channel_list_updated_at", datetime.now().isoformat())
            verify = ModelSetting.get("channel_list_cache") or ""
            logger.info("[SOOP_KBO][SCHED] DB저장 검증 saved_len=%d match=%s preview=%s",
                        len(verify), len(verify) == len(cache_json), verify[:80])
            logger.info("[SOOP_KBO][SCHED] %s", result_msg)
            # show.yaml 자동 갱신 (library_path 설정된 경우)
            ok, msg = _write_show_yaml()
            if ok:
                logger.info("[SOOP_KBO][SCHED] show.yaml 갱신: %s", msg)
                rok, rmsg = _trigger_plex_refresh()
                logger.info("[SOOP_KBO][SCHED] Plex refresh: ok=%s %s", rok, rmsg)
            else:
                logger.info("[SOOP_KBO][SCHED] show.yaml 건너뜀: %s", msg)
            # alive.yaml 채널명 업데이트
            aok, amsg = _update_alive_yaml()
            logger.info("[SOOP_KBO][SCHED] alive.yaml: ok=%s %s", aok, amsg)
        except Exception:
            logger.exception("[SOOP_KBO][SCHED] 실행 오류")
            ModelSetting.set("schedule_last_result", "error")
        finally:
            ModelSetting.set("schedule_last_run", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def process_ajax(self, sub, req):
        from flask import jsonify
        logger.info("[SOOP_KBO] process_ajax sub=%s", sub)
        try:
            if sub == "setting_save":
                saved, _ = P.ModelSetting.setting_save(req)
                if saved:
                    ok, msg = _write_show_yaml()
                    if ok:
                        logger.info("[SOOP_KBO] 설정 저장 후 show.yaml 갱신: %s", msg)
                return jsonify(saved)
            if sub == "cache_clear":
                with _cache_lock:
                    count = len(_cache)
                    _cache.clear()
                logger.info("[SOOP_KBO] 캐시 초기화: %d개", count)
                return jsonify({"count": count})
            if sub == "channel_list":
                raw = ModelSetting.get("channel_list_cache") or ""
                updated_at = ModelSetting.get("channel_list_updated_at") or ""
                logger.info("[SOOP_KBO][AJAX] channel_list raw_len=%d updated_at=%s preview=%s",
                            len(raw), updated_at, raw[:80])
                try:
                    rows = json.loads(raw) if raw else _fallback_rows_waiting()
                    logger.info("[SOOP_KBO][AJAX] channel_list parsed rows=%d titles=%s",
                                len(rows),
                                [r.get("program", {}).get("title", "") for r in rows])
                except Exception:
                    logger.exception("[SOOP_KBO][AJAX] channel_list JSON 파싱 실패")
                    rows = _fallback_rows_waiting()
                return jsonify({"list": rows, "updated_at": updated_at})
            if sub == "play_url":
                form = req.form.to_dict()
                channel_id = form.get("channel_id", "")
                urls = _load_channel_urls()
                if channel_id not in urls:
                    return jsonify({"data": None})
                pb = _proxy_base()
                url = f"{pb}/channel/{channel_id}.m3u8"
                return jsonify({"data": {"url": url, "title": CHANNEL_NAMES.get(channel_id, channel_id)}})
        except Exception:
            logger.exception("AJAX 처리 중 예외:")
            from datetime import datetime
            return jsonify({
                "list": _fallback_rows_waiting(),
                "updated_at": datetime.now().isoformat(),
                "error": "ajax_failed",
            }), 200


# ─── 라우트 ───────────────────────────────────────────────────────────────────
@blueprint.route("/ajax/check_section_db", methods=["POST"])
def soop_kbo_ajax_check_section_db():
    try:
        plugin = F.PluginManager.get_plugin_instance("plex_mate")
        if plugin is None or not hasattr(plugin, "PlexDBHandle") or not hasattr(plugin, "ModelSetting"):
            return jsonify({"ret": "warning", "msg": "plex_mate 플러그인을 찾을 수 없습니다"})
        db_file = (plugin.ModelSetting.get("base_path_db") or "").strip()
        if not db_file:
            return jsonify({"ret": "warning", "msg": "plex_mate base_path_db 설정이 비어 있습니다"})
        rows = plugin.PlexDBHandle.library_sections(db_file=db_file)
        if rows is None:
            return jsonify({"ret": "warning", "msg": f"DB 열기 실패: {db_file}"})
        return jsonify({
            "ret": "success",
            "title": f"Library sections ({db_file})",
            "modal": json.dumps(rows, indent=4, ensure_ascii=False),
        })
    except Exception as e:
        logger.exception("[SOOP_KBO] check_section_db 실패")
        return jsonify({"ret": "danger", "msg": f"조회 실패: {e}"})


@blueprint.route("/ajax/write_show_yaml", methods=["POST"])
def soop_kbo_ajax_write_show_yaml():
    ok, msg = _write_show_yaml()
    if ok:
        rok, rmsg = _trigger_plex_refresh()
        logger.info("[SOOP_KBO] Plex refresh: ok=%s %s", rok, rmsg)
        msg = f"{msg} | Plex: {rmsg}"
    aok, amsg = _update_alive_yaml()
    logger.info("[SOOP_KBO] alive.yaml: ok=%s %s", aok, amsg)
    if aok:
        msg = f"{msg} | alive: {amsg}"
    return jsonify({"ok": ok, "msg": msg})


@blueprint.route("/api/yaml")
def soop_kbo_api_yaml():
    try:
        import yaml as _yaml
    except ImportError:
        abort(500)
    from datetime import datetime
    stream_base_url = (ModelSetting.get("stream_base_url") or "").strip().rstrip("/")
    if not stream_base_url:
        stream_base_url = SystemModelSetting.get("ddns").rstrip("/")
    title_map: dict[str, str] = {}
    try:
        raw = ModelSetting.get("channel_list_cache") or ""
        if raw:
            for row in json.loads(raw):
                ch_id = row.get("channel_id", "")
                title = row.get("program", {}).get("title", "")
                if ch_id and title and "대기중" not in title:
                    title_map[ch_id] = title
    except Exception:
        logger.exception("[SOOP_KBO] api/yaml title_map 구성 실패")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    extras = []
    for i in range(1, 6):
        ch_id = f"kboglobal{i}"
        title = title_map.get(ch_id, f"SOOP KBO{i}")
        extras.append({
            "mode": "m3u8",
            "type": "featurette",
            "param": f"{stream_base_url}/soop_kbo/channel/{ch_id}.m3u8",
            "title": title,
            "thumb": f"https://raw.githubusercontent.com/zeliit/PlexLiveTV/main/thumb/soop_kbo{i}.png",
        })
    show = {
        "primary": True,
        "code": "kbo",
        "title": "숲 한국 프로야구",
        "posters": "https://raw.githubusercontent.com/zeliit/PlexLiveTV/main/poster/kbo.webp",
        "summary": f"SOOP KBO 채널\n\n  {now}",
        "extras": extras,
    }
    content = _yaml.safe_dump(show, allow_unicode=True, sort_keys=False)
    return Response(content, content_type="text/plain; charset=utf-8")


@blueprint.route("/ajax/channel_list_refresh", methods=["POST"])
def soop_kbo_ajax_channel_list_refresh():
    from datetime import datetime
    logger.info("[SOOP_KBO][AJAX-REFRESH] 즉시 조회 시작")
    try:
        rows, summary = _refresh_channel_titles("WEB")
        cache_json = json.dumps(rows, ensure_ascii=False)
        ModelSetting.set("channel_list_cache", cache_json)
        updated_at = datetime.now().isoformat()
        ModelSetting.set("channel_list_updated_at", updated_at)
        logger.info("[SOOP_KBO][AJAX-REFRESH] 완료 %s", summary)
        return jsonify({"list": rows, "updated_at": updated_at})
    except Exception:
        logger.exception("[SOOP_KBO][AJAX-REFRESH] 오류")
        return jsonify({"list": _fallback_rows_waiting(), "updated_at": datetime.now().isoformat()})


@blueprint.route("/ajax/channel_list", methods=["POST"])
def soop_kbo_ajax_channel_list():
    raw = ModelSetting.get("channel_list_cache") or ""
    updated_at = ModelSetting.get("channel_list_updated_at") or ""
    logger.info("[SOOP_KBO][AJAX-DIRECT] channel_list raw_len=%d updated_at=%s preview=%s",
                len(raw), updated_at, raw[:80])
    try:
        rows = json.loads(raw) if raw else _fallback_rows_waiting()
        logger.info("[SOOP_KBO][AJAX-DIRECT] parsed rows=%d titles=%s",
                    len(rows), [r.get("program", {}).get("title", "") for r in rows])
    except Exception:
        logger.exception("[SOOP_KBO][AJAX-DIRECT] JSON 파싱 실패")
        rows = _fallback_rows_waiting()
    return jsonify({"list": rows, "updated_at": updated_at})


@blueprint.route("/playlist.m3u8")
def soop_kbo_playlist():
    pb = _proxy_base()
    # DB 캐시에서 channel_id → title 매핑 구성
    title_map: dict[str, str] = {}
    try:
        raw = ModelSetting.get("channel_list_cache") or ""
        if raw:
            for row in json.loads(raw):
                ch_id = row.get("channel_id", "")
                title = row.get("program", {}).get("title", "")
                if ch_id and title and "대기중" not in title:
                    title_map[ch_id] = title
    except Exception:
        logger.exception("[SOOP_KBO] playlist title_map 구성 실패")

    lines = ["#EXTM3U", f"# SOOP_KBO_VERSION={PLUGIN_VERSION}"]
    for idx, ch in enumerate(_channel_list(), 1):
        display = title_map.get(ch["id"], ch["name"])
        lines.append(
            f'#EXTINF:-1 tvg-id="{ch["id"]}" tvg-name="{display}" '
            f'group-title="KBO" tvg-chno="{idx}",{display}'
        )
        lines.append(f"{pb}/channel/{ch['id']}.m3u8")
    return Response("\n".join(lines) + "\n", content_type="audio/mpegurl")


@blueprint.route("/channel/<channel_id>.m3u8")
def soop_kbo_channel(channel_id: str):
    urls = _load_channel_urls()
    if channel_id not in urls:
        abort(404)
    try:
        hls_url = _get_hls_url(channel_id)
        sess = _http_session()
        resp = sess.get(hls_url, timeout=15)
        resp.raise_for_status()
        return Response(_rewrite_m3u8(resp.text, hls_url, channel_id), content_type="application/vnd.apple.mpegurl")
    except Exception:
        logger.exception("[SOOP_KBO] 채널 오류: %s", channel_id)
        with _cache_lock:
            _cache.pop(channel_id, None)
        abort(503)


@blueprint.route("/sub")
def soop_kbo_sub():
    channel_id = request.args.get("c", "")
    encoded = request.args.get("url", "")
    if not encoded:
        abort(400)
    try:
        url = _b64dec(encoded)
        sess = _http_session()
        resp = sess.get(url, timeout=15)
        if resp.status_code == 403:
            logger.warning("[SOOP_KBO] 서브 플레이리스트 403 (토큰 만료) ch=%s → HLS 캐시 무효화", channel_id)
            if channel_id:
                with _cache_lock:
                    _cache.pop(channel_id, None)
            abort(503)
        resp.raise_for_status()
        return Response(
            _rewrite_m3u8(resp.text, url, channel_id),
            content_type="application/vnd.apple.mpegurl",
        )
    except Exception:
        logger.exception("[SOOP_KBO] 서브 플레이리스트 오류")
        abort(503)


@blueprint.route("/seg")
def soop_kbo_seg():
    channel_id = request.args.get("c", "")
    encoded = request.args.get("url", "")
    if not encoded:
        abort(400)
    try:
        url = _b64dec(encoded)
    except Exception:
        abort(400)
    try:
        if channel_id:
            now = time.time()
            with _quality_state_lock:
                last = _quality_log_last_ts.get(channel_id, 0.0)
                if now - last >= QUALITY_LOG_INTERVAL:
                    quality = _channel_quality.get(channel_id, "unknown")
                    logger.info("[SOOP_KBO] 채널=%s 재생품질=%s", channel_id, quality)
                    _quality_log_last_ts[channel_id] = now

        sess = _http_session()
        resp = sess.get(url, stream=True, timeout=30)
        resp.raise_for_status()

        def generate():
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk

        return Response(
            generate(),
            content_type=resp.headers.get("Content-Type", "video/MP2T"),
            headers={"Cache-Control": "no-cache"},
            direct_passthrough=True,
        )
    except Exception:
        logger.exception("[SOOP_KBO] 세그먼트 오류")
        abort(503)


@blueprint.route("/cache/clear")
def soop_kbo_cache_clear():
    with _cache_lock:
        count = len(_cache)
        _cache.clear()
    logger.info("[SOOP_KBO] 캐시 초기화: %d개", count)
    return f"SOOP KBO 캐시 {count}개 삭제 완료\n", 200


@blueprint.route("/proxy/check")
def soop_kbo_proxy_check():
    """현재 설정된 프록시의 외부 출구 IP를 확인."""
    try:
        proxy_url = _get_proxy_url()
        sess = _http_session()
        resp = sess.get("https://api.ipify.org?format=json", timeout=8)
        resp.raise_for_status()
        payload = resp.json() if resp.text else {}
        return jsonify({
            "ok": True,
            "proxy_url": proxy_url or "(직접 연결)",
            "egress_ip": payload.get("ip"),
        })
    except Exception as e:
        logger.exception("[SOOP_KBO] proxy check 실패")
        return jsonify({
            "ok": False,
            "error": str(e),
        }), 503
