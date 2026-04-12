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
from flask import Response, abort, render_template, request
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
    logger.info("[SOOP_KBO] API 응답 result: %s", data.get("result"))

    # result=1: 방송 중, result=-6: 방송 없음 등
    result_code = data.get("result", 0)
    if result_code != 1:
        raise RuntimeError(f"SOOP API result={result_code}: 방송 중이 아니거나 오류 ({bj_id})")

    channel = data.get("CHANNEL", {})
    hls_url = channel.get("RMD") or channel.get("CDN") or ""
    if not hls_url:
        # CDN 목록에서 직접 구성
        cdn_list = channel.get("CDN_LIST", [])
        logger.info("[SOOP_KBO] CDN_LIST: %s", cdn_list)
        raise RuntimeError(f"HLS URL을 찾을 수 없음. CHANNEL keys: {list(channel.keys())}")

    # RMD 값이 전체 m3u8 URL이면 그대로 사용, 아니면 조합
    if not hls_url.startswith("http"):
        bno = channel.get("BNO", "")
        auth_key = channel.get("AUTH_KEY", "")
        hls_url = f"https://{hls_url}/live/{bj_id}_{bno}/original/hls/playlist.m3u8?aid={auth_key}"

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
