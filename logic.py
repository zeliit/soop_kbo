"""
SOOP KBO HLS Proxy Plugin
=========================
SOOP KBO 스트림을 HLS 프록시로 제공합니다.

[ 엔드포인트 ]
  /soop_kbo/playlist.m3u8          - Plex / IPTV 플레이리스트
  /soop_kbo/channel/<id>.m3u8      - 채널 HLS 프록시
  /soop_kbo/sub                    - 서브 플레이리스트 프록시
  /soop_kbo/seg                    - 세그먼트 프록시
  /soop_kbo/cache/clear            - 스트림 캐시 초기화

[ 설정 (alive DB 설정 연동) ]
  streamlink_use_proxy  : true / false
  streamlink_proxy_url  : http://user:pass@host:port
"""
import re
import threading
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from urllib.parse import urljoin, urlparse

import requests
from flask import Response, abort, request
from plugin import F, PluginModuleBase  # type: ignore # pylint: disable=import-error

from .setup import P

logger = P.logger
package_name = P.package_name
ModelSetting = P.ModelSetting
blueprint = P.blueprint
SystemModelSetting = F.SystemModelSetting

# ─── 채널 목록 ────────────────────────────────────────────────────────────────
KBO_CHANNELS = {
    "kboglobal1": {"name": "SOOP KBO1", "url": "https://play.sooplive.co.kr/kboglobal/1"},
    "kboglobal2": {"name": "SOOP KBO2", "url": "https://play.sooplive.co.kr/kboglobal/2"},
    "kboglobal3": {"name": "SOOP KBO3", "url": "https://play.sooplive.co.kr/kboglobal/3"},
    "kboglobal4": {"name": "SOOP KBO4", "url": "https://play.sooplive.co.kr/kboglobal/4"},
    "kboglobal5": {"name": "SOOP KBO5", "url": "https://play.sooplive.co.kr/kboglobal/5"},
}

# ─── 스트림 캐시 ──────────────────────────────────────────────────────────────
_cache: dict = {}
_cache_lock = threading.Lock()
CACHE_TTL = 1800  # 30분


# ─── 설정 헬퍼 ───────────────────────────────────────────────────────────────
def _proxy_url() -> str | None:
    try:
        if ModelSetting.get_bool("proxy_use"):
            return ModelSetting.get("proxy_url") or None
    except Exception:
        pass
    return None


def _http_session() -> requests.Session:
    sess = requests.Session()
    sess.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    if purl := _proxy_url():
        sess.proxies.update({"http": purl, "https": purl})
    return sess


# ─── 스트림 관리 ─────────────────────────────────────────────────────────────
def _get_stream(channel_id: str):
    """스트림 캐시에서 가져오거나 streamlink로 새로 획득."""
    with _cache_lock:
        if channel_id in _cache:
            stream, cached_at = _cache[channel_id]
            if time.time() - cached_at < CACHE_TTL:
                return stream
            del _cache[channel_id]

    from streamlink import Streamlink  # type: ignore # pylint: disable=import-error

    ch = KBO_CHANNELS[channel_id]
    sl = Streamlink()
    if purl := _proxy_url():
        sl.set_option("http-proxy", purl)

    logger.info("[SOOP_KBO] 스트림 획득 중: %s", ch["url"])
    streams = sl.streams(ch["url"])
    if not streams:
        raise RuntimeError(f"스트림 없음: {ch['url']}")
    stream = streams.get("best")
    if stream is None:
        raise RuntimeError(f"best 스트림 없음. 가능한 품질: {list(streams)}")

    logger.info("[SOOP_KBO] 스트림 획득 성공: %s / %s", channel_id, type(stream).__name__)
    with _cache_lock:
        _cache[channel_id] = (stream, time.time())
    return stream


def _get_hls_url(stream) -> str:
    try:
        from streamlink.stream.hls import MuxedHLSStream  # type: ignore # pylint: disable=import-error

        if isinstance(stream, MuxedHLSStream):
            logger.info("[SOOP_KBO] MuxedHLSStream - 비디오 서브스트림 사용")
            return stream.substreams[0].url
    except (ImportError, AttributeError, IndexError):
        pass
    if hasattr(stream, "url"):
        return stream.url
    raise NotImplementedError(f"지원하지 않는 스트림 타입: {type(stream).__name__}")


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


# ─── Logic 클래스 (플러그인 프레임워크 필수) ─────────────────────────────────
class Logic(PluginModuleBase):
    db_default = {
        "proxy_use": "False",
        "proxy_url": "",
    }

    def __init__(self, PM):
        super().__init__(PM, None)
        self.name = "setting"

    def process_menu(self, sub, req):
        from flask import render_template
        try:
            arg = ModelSetting.to_dict()
            arg["package_name"] = package_name
            arg["playlist_url"] = f"{SystemModelSetting.get('ddns')}/{package_name}/playlist.m3u8"
            return render_template(f"{package_name}_{sub}.html", sub=sub, arg=arg)
        except Exception:
            logger.exception("메뉴 처리 중 예외:")
            return render_template("sample.html", title=f"{package_name} - {sub}")

    def process_ajax(self, sub, req):
        from flask import jsonify
        try:
            if sub == "setting_save":
                saved, _ = ModelSetting.setting_save(req)
                return jsonify(saved)
            if sub == "cache_clear":
                with _cache_lock:
                    count = len(_cache)
                    _cache.clear()
                logger.info("[SOOP_KBO] 캐시 초기화: %d개", count)
                return jsonify({"count": count})
        except Exception:
            logger.exception("AJAX 처리 중 예외:")


# ─── 라우트 ───────────────────────────────────────────────────────────────────
@blueprint.route("/playlist.m3u8")
def soop_kbo_playlist():
    pb = _proxy_base()
    lines = ["#EXTM3U"]
    for idx, (ch_id, ch) in enumerate(KBO_CHANNELS.items(), 1):
        lines.append(
            f'#EXTINF:-1 tvg-id="{ch_id}" tvg-name="{ch["name"]}" '
            f'group-title="KBO" tvg-chno="{idx}",{ch["name"]}'
        )
        lines.append(f"{pb}/channel/{ch_id}.m3u8")
    return Response("\n".join(lines) + "\n", content_type="audio/mpegurl")


@blueprint.route("/channel/<channel_id>.m3u8")
def soop_kbo_channel(channel_id: str):
    if channel_id not in KBO_CHANNELS:
        abort(404)
    try:
        stream = _get_stream(channel_id)
        hls_url = _get_hls_url(stream)
        logger.info("[SOOP_KBO] HLS URL: %.80s", hls_url)

        sess = _http_session()
        resp = sess.get(hls_url, timeout=15)
        resp.raise_for_status()

        return Response(
            _rewrite_m3u8(resp.text, hls_url, channel_id),
            content_type="application/vnd.apple.mpegurl",
        )
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
