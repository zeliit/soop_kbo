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
from base64 import urlsafe_b64decode, urlsafe_b64encode
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
    return result


# ─── 스트림 캐시 ──────────────────────────────────────────────────────────────
_cache: dict = {}
_cache_lock = threading.Lock()
CACHE_TTL = 1800  # 30분


# ─── 설정 헬퍼 ───────────────────────────────────────────────────────────────
def _required_proxy_url() -> str:
    """SOOP KBO는 해외 IP가 필요하므로 프록시를 필수로 강제."""
    purl = (ModelSetting.get("proxy_url") or "").strip()
    if not purl:
        raise RuntimeError(
            "[SOOP_KBO] proxy_url 미설정. "
            "SOOP KBO는 해외 프록시가 필수입니다."
        )
    return purl


def _http_session() -> requests.Session:
    sess = requests.Session()
    sess.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    purl = _required_proxy_url()
    sess.proxies.update({"http": purl, "https": purl})
    return sess


# ─── SOOP API 직접 호출 ───────────────────────────────────────────────────────
def _parse_bj_id(page_url: str) -> str:
    """https://play.sooplive.co.kr/<bjid> 또는 www.sooplive.co.kr/<bjid> 에서 BJ ID 추출."""
    path = urlparse(page_url).path.strip("/")
    # path 예: "kboglobal1" 또는 "kboglobal1/1" → 첫 번째 세그먼트
    return path.split("/")[0]


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


def _get_hls_url_from_api(page_url: str, sess: requests.Session) -> str:
    """SOOP 방송국 API에서 HLS URL 획득."""
    bj_id = _parse_bj_id(page_url)
    logger.info("[SOOP_KBO] BJ ID: %s / page_url: %s", bj_id, page_url)

    # 1) 라이브 정보 API
    api_url = "https://live.sooplive.co.kr/afreeca/player_live_api.php"
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
        "Origin": "https://play.sooplive.co.kr",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    resp = sess.post(api_url, data=payload, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()
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
    # SOOP 응답마다 토큰 키가 달라서 가능한 키를 순차 확인
    aid = (
        str(channel.get("AUTH_KEY", "")).strip()
        or str(channel.get("AID", "")).strip()
        or str(channel.get("FTK", "")).strip()
    )

    # 1) 이미 완전한 m3u8 URL이면 그대로 사용
    if hls_url.startswith("http") and _looks_like_m3u8_url(hls_url):
        logger.info("[SOOP_KBO] 직접 m3u8 URL 사용")
        return hls_url

    # 2) 도메인/호스트만 내려오는 케이스 보강
    host = hls_url
    if not host.startswith("http"):
        host = f"https://{host}"
    host = host.rstrip("/")

    candidates = []
    if bno:
        base_path = f"/live/{bj_id}_{bno}/original/hls/playlist.m3u8"
        if aid:
            candidates.append(f"{host}{base_path}?aid={aid}")
        candidates.append(f"{host}{base_path}")
        candidates.append(f"{host}/live/{bj_id}_{bno}/playlist.m3u8")
        candidates.append(f"{host}/live/{bj_id}_{bno}/master.m3u8")

    logger.info("[SOOP_KBO] HLS 후보 URL 개수: %d", len(candidates))
    for cand in candidates:
        if _probe_m3u8_url(sess, cand):
            logger.info("[SOOP_KBO] HLS 후보 성공: %.120s", cand)
            return cand

    # 3) 마지막 fallback: 원본 값 반환 (아래 단계에서 상세 HTTP 오류 확인 가능)
    logger.warning("[SOOP_KBO] HLS 후보 탐색 실패, 원본 URL fallback: %.120s", host)
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
        super(ModuleMain, self).__init__(P, name="main", first_menu="setting")
        self.db_default = {
            "proxy_use": "True",
            "proxy_url": "",
            "channel_urls": json.dumps(DEFAULT_CHANNEL_URLS, ensure_ascii=False),
        }

    def process_menu(self, sub, _req):
        try:
            arg = P.ModelSetting.to_dict()
            arg["package_name"] = P.package_name
            arg["plugin_version"] = PLUGIN_VERSION
            arg["playlist_url"] = f"{SystemModelSetting.get('ddns')}/{P.package_name}/playlist.m3u8"
            arg["default_channel_urls"] = json.dumps(DEFAULT_CHANNEL_URLS, ensure_ascii=False, indent=2)
            return render_template(f"{P.package_name}_{self.name}_{sub}.html", arg=arg)
        except Exception:
            logger.exception("메뉴 처리 중 예외:")
            return render_template("sample.html", title=f"{P.package_name} - {sub}")

    def process_ajax(self, sub, req):
        from flask import jsonify
        logger.info("[SOOP_KBO] process_ajax sub=%s", sub)
        try:
            if sub == "setting_save":
                saved, _ = P.ModelSetting.setting_save(req)
                return jsonify(saved)
            if sub == "cache_clear":
                with _cache_lock:
                    count = len(_cache)
                    _cache.clear()
                logger.info("[SOOP_KBO] 캐시 초기화: %d개", count)
                return jsonify({"count": count})
            if sub == "channel_list":
                rows = [
                    {
                        "source": "soop_kbo",
                        "channel_id": ch["id"],
                        "name": ch["name"],
                        "program": {"title": "LIVE"},
                    }
                    for ch in _channel_list()
                ]
                from datetime import datetime
                return jsonify({"list": rows, "updated_at": datetime.now().isoformat()})
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


# ─── 라우트 ───────────────────────────────────────────────────────────────────
@blueprint.route("/playlist.m3u8")
def soop_kbo_playlist():
    pb = _proxy_base()
    lines = ["#EXTM3U", f"# SOOP_KBO_VERSION={PLUGIN_VERSION}"]
    for idx, ch in enumerate(_channel_list(), 1):
        lines.append(
            f'#EXTINF:-1 tvg-id="{ch["id"]}" tvg-name="{ch["name"]}" '
            f'group-title="KBO" tvg-chno="{idx}",{ch["name"]}'
        )
        lines.append(f"{pb}/channel/{ch['id']}.m3u8")
    return Response("\n".join(lines) + "\n", content_type="audio/mpegurl")


@blueprint.route("/channel/<channel_id>.m3u8")
def soop_kbo_channel(channel_id: str):
    logger.info("[SOOP_KBO] 채널 요청: %s", channel_id)
    urls = _load_channel_urls()
    if channel_id not in urls:
        abort(404)
    try:
        hls_url = _get_hls_url(channel_id)
        logger.info("[SOOP_KBO] HLS URL: %.120s", hls_url)

        sess = _http_session()
        resp = sess.get(hls_url, timeout=15)
        logger.info("[SOOP_KBO] m3u8 응답: status=%s content-type=%s", resp.status_code, resp.headers.get("Content-Type"))
        resp.raise_for_status()

        rewritten = _rewrite_m3u8(resp.text, hls_url, channel_id)
        logger.info("[SOOP_KBO] m3u8 재작성 완료 (%d bytes)", len(rewritten))
        return Response(rewritten, content_type="application/vnd.apple.mpegurl")
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
    logger.info("[SOOP_KBO] 세그먼트 요청 from %s", request.remote_addr)
    encoded = request.args.get("url", "")
    if not encoded:
        abort(400)
    try:
        url = _b64dec(encoded)
    except Exception:
        abort(400)
    try:
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
        proxy_url = _required_proxy_url()
        sess = _http_session()
        resp = sess.get("https://api.ipify.org?format=json", timeout=8)
        resp.raise_for_status()
        payload = resp.json() if resp.text else {}
        return jsonify({
            "ok": True,
            "proxy_url": proxy_url,
            "egress_ip": payload.get("ip"),
        })
    except Exception as e:
        logger.exception("[SOOP_KBO] proxy check 실패")
        return jsonify({
            "ok": False,
            "error": str(e),
        }), 503
