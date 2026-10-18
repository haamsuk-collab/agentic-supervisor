"""Phase 0 — official public-API router (the SANCTIONED exception to No-Site-Name).

Per SKILL.md R5, platforms that publish official no-auth public endpoints get a
deterministic route tried BEFORE the generic WAF grid. This is the *enforced,
in-engine* version of what used to be agent-driven curl snippets in SKILL.md —
so the agent can no longer silently skip it (which is exactly how Reddit/X were
wrongly declared "blocked": the grid 403'd on `.json` and nobody tried `.rss`).

This file is the ONLY engine/ module allowed to name platform hosts; it is
exempted in `bias_check.EXPLICIT_ALLOW_FILES`. Do NOT add per-site logic to any
other engine file — generic WAF handling stays site-agnostic.

Contract:
    route(url) -> Optional[dict]
      None              → url is not a recognised Phase-0 platform; caller runs
                          the generic grid as usual.
      {"platform","ok","route","content","final_url","attempts":[...]}
                        → recognised platform. `ok` says whether an official
                          route succeeded. Even on ok=False the caller should
                          fall through to the grid, but `attempts` is recorded
                          so failure is never silent.

Each attempt dict: {"route","platform","ok","status","bytes","note"}.
"""
from __future__ import annotations

from datetime import datetime, timezone
import html as html_lib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit


# --- low-level helpers -------------------------------------------------------
def _cffi_get(url: str, *, impersonate: str = "safari", timeout: int = 15):
    from curl_cffi import requests as r  # lazy: engine works even if missing
    return r.get(
        url,
        impersonate=impersonate,  # type: ignore[arg-type]
        timeout=timeout,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
        },
        allow_redirects=True,
    )


def _cffi_post(url: str, *, data: Optional[dict] = None, headers: Optional[dict] = None,
               cookies: Optional[dict] = None, impersonate: str = "safari", timeout: int = 15):
    from curl_cffi import requests as r  # lazy: engine works even if missing
    req_headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if headers:
        req_headers.update(headers)
    try:
        from .transport import POOL, pool_enabled, _host_of
        if pool_enabled():
            ent = POOL.get(_host_of(url), impersonate)
            if ent is not None:
                if cookies:
                    for k, v in cookies.items():
                        try:
                            ent.session.cookies.set(k, v)
                        except Exception:
                            pass
                return ent.session.post(url, data=data, headers=req_headers, timeout=timeout)
    except Exception:
        pass
    return r.post(
        url,
        data=data,
        headers=req_headers,
        cookies=cookies,
        impersonate=impersonate,  # type: ignore[arg-type]
        timeout=timeout,
        allow_redirects=True,
    )

def _host(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h  # strip the literal "www." prefix only


def _attempt(platform: str, route: str, ok: bool, status: int, body: str, note: str = "") -> dict:
    return {"platform": platform, "route": route, "ok": ok, "status": status,
            "bytes": len(body or ""), "note": note}


# --- platform detectors ------------------------------------------------------
def _detect(url: str) -> Optional[str]:
    h = _host(url)
    if not h:
        return None
    if "reddit.com" in h or h == "redd.it":
        return "reddit"
    if h in ("x.com", "twitter.com") or h.endswith(".x.com") or h.endswith(".twitter.com"):
        return "x"
    if "youtube.com" in h or h == "youtu.be":
        return "youtube"
    if h in ("threads.com", "threads.net") or h.endswith((".threads.com", ".threads.net")):
        return "threads"
    return None


# --- reddit ------------------------------------------------------------------
def _reddit(url: str, timeout: int) -> dict:
    attempts: list[dict] = []
    base = url.split("?", 1)[0].rstrip("/")
    # Build an .rss / .json target from the path (works for /r/<sub> and post URLs).
    rss_url = base + ("/.rss" if "/comments/" not in base else ".rss")
    json_url = base + ("/.json" if "/comments/" not in base else ".json")

    # Route 1: RSS (the route that actually survives — Reddit gates the JSON API).
    try:
        x = _cffi_get(rss_url, timeout=timeout)
        ok = x.status_code == 200 and ("<rss" in x.text or "<feed" in x.text)
        attempts.append(_attempt("reddit", "rss", ok, x.status_code, x.text,
                                 "feed" if ok else "no-feed-markers"))
        if ok:
            return {"platform": "reddit", "ok": True, "route": "rss",
                    "content": x.text, "final_url": rss_url, "attempts": attempts}
    except Exception as e:
        attempts.append(_attempt("reddit", "rss", False, 0, "", f"{type(e).__name__}"))

    # Route 2: JSON via curl_cffi (often 403 now, but try — cheap).
    try:
        x = _cffi_get(json_url, timeout=timeout)
        ok = x.status_code == 200 and x.text.lstrip().startswith(("{", "["))
        attempts.append(_attempt("reddit", "json", ok, x.status_code, x.text,
                                 "json" if ok else f"status={x.status_code}"))
        if ok:
            return {"platform": "reddit", "ok": True, "route": "json",
                    "content": x.text, "final_url": json_url, "attempts": attempts}
    except Exception as e:
        attempts.append(_attempt("reddit", "json", False, 0, "", f"{type(e).__name__}"))

    return {"platform": "reddit", "ok": False, "route": None, "content": "",
            "final_url": url, "attempts": attempts}


# --- x / twitter -------------------------------------------------------------
_TWEET_ID_RE = re.compile(r"/status(?:es)?/(\d+)")
_TCO_ONLY_RE = re.compile(r"^\s*https://t\.co/\S+\s*$")
_LONG_TEXT_CHARS = 270


def _x_truncation_signal(d: dict) -> bool:
    """tweet-result JSON shows the text may be cut: long-form note_tweet, an X
    Article, near-280 text, or only a t.co link (2026-10-08: these 4 signals
    covered 6/6 long/article tweets and 0/12 plain short tweets)."""
    text = d.get("text") or ""
    return ("note_tweet" in d or "article" in d or len(text) >= _LONG_TEXT_CHARS
            or bool(_TCO_ONLY_RE.match(text)))


def _fxtwitter_full_text(fx: dict) -> str:
    t = (fx or {}).get("tweet") or {}
    art = t.get("article") or {}
    blocks = ((art.get("content") or {}).get("blocks")) or []
    body = "\n\n".join(b.get("text", "") for b in blocks if b.get("text"))
    if body:
        return "\n\n".join(x for x in (art.get("title") or "", body) if x)
    return t.get("text") or ""


def _x_fxtwitter(tid: str, d: dict, timeout: int, attempts: list[dict]) -> Optional[str]:
    """Third-party fallback (api.fxtwitter.com — the tweet URL leaves to that
    service; disable with INSANE_SEARCH_FXTWITTER=0). Returns full text only if
    it is longer than what tweet-result already gave; None otherwise."""
    user = ((d.get("user") or {}).get("screen_name")) or "i"
    try:
        x = _cffi_get(f"https://api.fxtwitter.com/{user}/status/{tid}", impersonate="chrome", timeout=timeout)
        full = _fxtwitter_full_text(x.json()) if x.status_code == 200 else ""
        ok = len(full) > len(d.get("text") or "")
        attempts.append(_attempt("x", "fxtwitter", ok, x.status_code, x.text,
                                 f"chars={len(full)}" if ok else f"status={x.status_code} not-longer"))
        return full if ok else None
    except Exception as e:
        attempts.append(_attempt("x", "fxtwitter", False, 0, "", f"{type(e).__name__}"))
        return None


def _x(url: str, timeout: int) -> dict:
    attempts: list[dict] = []
    m = _TWEET_ID_RE.search(url)

    if m:  # single tweet → tweet-result + oembed (both no-auth, reliable)
        tid = m.group(1)
        try:
            x = _cffi_get(f"https://cdn.syndication.twimg.com/tweet-result?id={tid}&token=a", timeout=timeout)
            d = x.json() if x.status_code == 200 else {}
            ok = bool(d.get("text"))
            attempts.append(_attempt("x", "tweet-result", ok, x.status_code, x.text,
                                     "has-text" if ok else f"status={x.status_code}"))
            if ok:
                res = {"platform": "x", "ok": True, "route": "tweet-result",
                       "content": x.text, "final_url": url, "attempts": attempts}
                if _x_truncation_signal(d):
                    if os.environ.get("INSANE_SEARCH_FXTWITTER", "1") == "0":
                        res.update(truncated_possible=True, truncation_reason="fxtwitter_disabled")
                    else:
                        full = _x_fxtwitter(tid, d, timeout, attempts)
                        if full:
                            res.update(route="fxtwitter", content=full)
                        else:
                            res.update(truncated_possible=True, truncation_reason="fxtwitter_failed")
                return res
        except Exception as e:
            attempts.append(_attempt("x", "tweet-result", False, 0, "", f"{type(e).__name__}"))
        try:
            ourl = f"https://publish.twitter.com/oembed?url=https://twitter.com/i/status/{tid}&omit_script=1"
            x = _cffi_get(ourl, timeout=timeout)
            d = x.json() if x.status_code == 200 else {}
            ok = bool(d.get("html"))
            attempts.append(_attempt("x", "oembed", ok, x.status_code, x.text,
                                     "has-html" if ok else f"status={x.status_code}"))
            if ok:
                return {"platform": "x", "ok": True, "route": "oembed",
                        "content": x.text, "final_url": ourl, "attempts": attempts}
        except Exception as e:
            attempts.append(_attempt("x", "oembed", False, 0, "", f"{type(e).__name__}"))
    else:  # profile timeline → syndication (rate-limit-prone; retry once)
        handle = urlsplit(url).path.strip("/").split("/")[0]
        _reserved = {"i", "search", "home", "explore", "messages", "notifications", "settings", "hashtag"}
        if handle and handle.lower() not in _reserved:
            surl = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}"
            for attempt_no in range(2):
                try:
                    x = _cffi_get(surl, timeout=timeout)
                    ok = x.status_code == 200 and "__NEXT_DATA__" in x.text
                    attempts.append(_attempt("x", f"syndication-timeline#{attempt_no+1}", ok,
                                             x.status_code, x.text,
                                             "timeline" if ok else f"status={x.status_code}"))
                    if ok:
                        return {"platform": "x", "ok": True, "route": "syndication-timeline",
                                "content": x.text, "final_url": surl, "attempts": attempts}
                except Exception as e:
                    attempts.append(_attempt("x", f"syndication-timeline#{attempt_no+1}", False, 0, "", f"{type(e).__name__}"))

    return {"platform": "x", "ok": False, "route": None, "content": "",
            "final_url": url, "attempts": attempts}


# --- youtube -----------------------------------------------------------------
def _ytdlp_argv() -> Optional[list[str]]:
    """Best yt-dlp invocation for this environment, or None if unavailable.

    Prefers the ``yt-dlp`` console script on PATH; falls back to
    ``<python> -m yt_dlp`` when the script dir is not on PATH but the module is
    importable — the common case for ``pip install --user`` and Windows / venv
    installs, where the Scripts/bin dir is frequently absent from PATH. Mirrors
    the ``which yt-dlp || python3 -m yt_dlp`` fallback already documented in
    references/media.md."""
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]
    return None


def _youtube(url: str, timeout: int) -> dict:
    attempts: list[dict] = []
    argv = _ytdlp_argv()
    if argv is None:
        attempts.append(_attempt("youtube", "yt-dlp", False, 0, "", "yt-dlp not installed"))
        return {"platform": "youtube", "ok": False, "route": None, "content": "",
                "final_url": url, "attempts": attempts}
    try:
        p = subprocess.run(
            argv + ["--dump-json", "--skip-download", url],
            capture_output=True, text=True, timeout=max(timeout, 60),
        )
        ok = p.returncode == 0 and p.stdout.strip().startswith("{")
        note = "json" if ok else (p.stderr or "").strip()[:80]
        attempts.append(_attempt("youtube", "yt-dlp", ok, 200 if ok else 0, p.stdout, note))
        if ok:
            return {"platform": "youtube", "ok": True, "route": "yt-dlp",
                    "content": p.stdout, "final_url": url, "attempts": attempts}
    except FileNotFoundError:
        attempts.append(_attempt("youtube", "yt-dlp", False, 0, "", "yt-dlp not installed"))
    except Exception as e:
        attempts.append(_attempt("youtube", "yt-dlp", False, 0, "", f"{type(e).__name__}"))
    return {"platform": "youtube", "ok": False, "route": None, "content": "",
            "final_url": url, "attempts": attempts}


# --- threads -----------------------------------------------------------------
_THREADS_POST_RE = re.compile(r"/(?:post|t)/([A-Za-z0-9_-]+)")
_THREADS_SHARE_RE = re.compile(r"/share/([A-Za-z0-9_-]+)")
_THREADS_USER_POST_RE = re.compile(r"/(?:@|&#064;)([^/?#\"'\s]+)/post/([A-Za-z0-9_-]+)")
_THREADS_SJS_RE = re.compile(r"<script[^>]*\bdata-sjs\b[^>]*>(.*?)</script>", re.DOTALL)
_THREADS_LSD_RE = re.compile(r'"LSD"\s*,\s*\[\]\s*,\s*\{\s*"token"\s*:\s*"([^"]+)"')
_THREADS_GRAPHQL_URL = "https://www.threads.com/api/graphql"
_THREADS_IG_APP_ID = "238260118697367"
_THREADS_DOWNWARD_DOC_ID = "29326538906932542"
_THREADS_REFETCH_DOC_ID = "29200133479591970"
_THREADS_TARGET_DOC_ID = "29059532420308282"
_THREADS_SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_THREADS_MAX_MARKDOWN_BYTES = 19000

_DEFAULT_THREADS_DOWNWARD_VARS: dict[str, Any] = {
    "__relay_internal__pv__BarcelonaHasBestOfThreadsrelayprovider": True,
    "__relay_internal__pv__BarcelonaHasDearAlgoConsumptionrelayprovider": True,
    "__relay_internal__pv__BarcelonaHasMetaAiContentAttachmentsrelayprovider": False,
    "__relay_internal__pv__BarcelonaHasMWRLocationConsumptionrelayprovider": False,
    "__relay_internal__pv__BarcelonaShouldFetchPostAuthorFullNamerelayprovider": False,
    "__relay_internal__pv__BarcelonaIsLoggedInrelayprovider": False,
    "__relay_internal__pv__BarcelonaMessagesHasLiveChatMessagingrelayprovider": False,
    "__relay_internal__pv__BarcelonaHasEventBadgerelayprovider": False,
    "__relay_internal__pv__BarcelonaMessagingHasMetaAIBotrelayprovider": False,
    "__relay_internal__pv__BarcelonaGenAIRepliesEnabledrelayprovider": False,
    "__relay_internal__pv__BarcelonaIsSearchDiscoveryEnabledrelayprovider": False,
    "__relay_internal__pv__BarcelonaHasCommunitiesrelayprovider": True,
    "__relay_internal__pv__BarcelonaHasGameScoreSharerelayprovider": True,
    "__relay_internal__pv__BarcelonaHasPublicViewCountCardrelayprovider": True,
    "__relay_internal__pv__BarcelonaHasCommunityEmojiUpdateCardrelayprovider": True,
    "__relay_internal__pv__BarcelonaHasCommunityEntityCardrelayprovider": False,
    "__relay_internal__pv__BarcelonaHasScorecardCommunityrelayprovider": False,
    "__relay_internal__pv__BarcelonaHasSportTeamAllegianceCardrelayprovider": False,
    "__relay_internal__pv__BarcelonaHasStitchedAttachmentrelayprovider": False,
    "__relay_internal__pv__BarcelonaHasNewspaperLinkStylerelayprovider": False,
    "__relay_internal__pv__BarcelonaHasMessagingrelayprovider": False,
    "__relay_internal__pv__BarcelonaOptionalCookiesEnabledrelayprovider": True,
    "__relay_internal__pv__BarcelonaShouldFulfillLightboxQueryrelayprovider": True,
    "__relay_internal__pv__BarcelonaCanSeeSponsoredContentrelayprovider": False,
    "__relay_internal__pv__BarcelonaHasUnseenRepliesrelayprovider": False,
    "__relay_internal__pv__BarcelonaHasWebFaviconsrelayprovider": True,
    "__relay_internal__pv__BarcelonaIsCrawlerrelayprovider": False,
    "__relay_internal__pv__BarcelonaHasDearAlgoWebProductionrelayprovider": False,
    "__relay_internal__pv__BarcelonaHasCommunityTopContributorsrelayprovider": False,
    "__relay_internal__pv__BarcelonaHasCustomLikesConsumptionrelayprovider": False,
    "__relay_internal__pv__BarcelonaHasViewerRepliedrelayprovider": True,
    "__relay_internal__pv__BarcelonaHasPrivateRepliesDeprecationrelayprovider": False,
    "__relay_internal__pv__BarcelonaHasGhostPostEmojiActivationrelayprovider": False,
    "__relay_internal__pv__BarcelonaHasGhostPostProductionrelayprovider": False,
    "__relay_internal__pv__BarcelonaHasPodcastTranscriptConsumptionrelayprovider": True,
    "__relay_internal__pv__BarcelonaHasPodcastV2Consumptionrelayprovider": True,
    "__relay_internal__pv__BarcelonaHasNewsletterLinkPreviewrelayprovider": False,
    "__relay_internal__pv__BarcelonaHasPublicViewCountShareCardrelayprovider": False,
    "__relay_internal__pv__BarcelonaShouldShowFediverseM075Featuresrelayprovider": False,
    "__relay_internal__pv__BarcelonaIsInternalUserrelayprovider": False,
    "__relay_internal__pv__BarcelonaHasPermalinkIndentationrelayprovider": False,
}


def _shortcode_to_pk(code: str) -> str:
    if not code:
        return ""
    n = 0
    for ch in code:
        idx = _THREADS_SHORTCODE_ALPHABET.find(ch)
        if idx < 0:
            return ""
        n = (n << 6) | idx
    return str(n) if n > 0 else ""


def _walk_threads_sjs(obj: Any, relays: dict[str, dict], preloaders: list[dict]) -> None:
    if isinstance(obj, dict):
        if isinstance(obj.get("expectedPreloaders"), list):
            for item in obj["expectedPreloaders"]:
                if isinstance(item, dict):
                    preloaders.append(item)
        for v in obj.values():
            _walk_threads_sjs(v, relays, preloaders)
    elif isinstance(obj, list):
        if (len(obj) >= 4 and obj[0] == "RelayPrefetchedStreamCache"
                and obj[1] == "next" and isinstance(obj[3], list) and len(obj[3]) >= 2):
            preloader_id = obj[3][0]
            bbox = obj[3][1]
            if isinstance(preloader_id, str) and isinstance(bbox, dict):
                data = ((bbox.get("__bbox") or {}).get("result") or {}).get("data")
                if isinstance(data, dict):
                    relays[preloader_id] = data
        else:
            for item in obj:
                _walk_threads_sjs(item, relays, preloaders)


def _parse_threads_html(raw: str) -> tuple[str, list[dict], dict[str, dict]]:
    lsd_m = _THREADS_LSD_RE.search(raw)
    lsd = lsd_m.group(1) if lsd_m else ""
    relays: dict[str, dict] = {}
    preloaders: list[dict] = []
    for block in _THREADS_SJS_RE.findall(raw):
        try:
            parsed = json.loads(block)
        except Exception:
            continue
        _walk_threads_sjs(parsed, relays, preloaders)
    return lsd, preloaders, relays


def _threads_post_text(post: dict) -> str:
    cap = (post.get("caption") or {}).get("text")
    if cap:
        return str(cap).strip()
    tpa = post.get("text_post_app_info") or {}
    frags = ((tpa.get("text_fragments") or {}).get("fragments") or [])
    return "".join(str((f or {}).get("plaintext") or "") for f in frags).strip()


def _threads_image_urls(post: dict) -> list[str]:
    urls: list[str] = []
    cands = ((post.get("image_versions2") or {}).get("candidates") or [])
    if cands:
        best = max(cands, key=lambda c: (int((c or {}).get("width") or 0) * int((c or {}).get("height") or 0)))
        u = (best or {}).get("url") or (cands[0] or {}).get("url")
        if u:
            urls.append(str(u))
    for item in (post.get("carousel_media") or []):
        icands = (((item or {}).get("image_versions2") or {}).get("candidates") or [])
        if icands:
            best = max(icands, key=lambda c: (int((c or {}).get("width") or 0) * int((c or {}).get("height") or 0)))
            u = (best or {}).get("url") or (icands[0] or {}).get("url")
            if u and str(u) not in urls:
                urls.append(str(u))
    return urls


def _threads_video_urls(post: dict) -> list[str]:
    urls: list[str] = []
    vids = post.get("video_versions") or []
    if vids and isinstance(vids[0], dict) and vids[0].get("url"):
        urls.append(str(vids[0]["url"]))
    for item in (post.get("carousel_media") or []):
        ivids = (item or {}).get("video_versions") or []
        if ivids and isinstance(ivids[0], dict) and ivids[0].get("url"):
            u = str(ivids[0]["url"])
            if u not in urls:
                urls.append(u)
    return urls


def _format_threads_markdown(
    root_pk: str,
    posts_by_pk: dict[str, dict],
    children_map: dict[str, list[str]],
    direct_only_children_map: dict[str, list[str]],
    has_unavail_map: dict[str, bool],
    unavailable_nodes: list[str],
    final_url: str,
) -> str:
    root = posts_by_pk[root_pk]
    user = root.get("user") or {}
    uname = str(user.get("username") or "unknown")
    fname = str(user.get("full_name") or "")
    code = str(root.get("code") or "")
    taken_at = root.get("taken_at")
    if isinstance(taken_at, (int, float)) and taken_at > 0:
        date_str = datetime.fromtimestamp(taken_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    else:
        date_str = "unknown"
    tpa = root.get("text_post_app_info") or {}
    thread_reply_count = int(tpa.get("direct_reply_count") or 0)

    author_hdr = f"@{uname}" + (f" ({fname})" if fname and fname != uname else "")
    lines: list[str] = [
        f"# Threads Post by {author_hdr}",
        f"- Post ID: `{code}` (`id={root_pk}`)",
        f"- Author: {author_hdr} (`user_id={user.get('pk') or user.get('id') or ''}`)",
        f"- Date: {date_str}",
        f"- URL: {final_url}",
        f"- Stats: likes={root.get('like_count') or 0}, replies={thread_reply_count}, "
        f"reposts={tpa.get('repost_count') or 0}, quotes={tpa.get('quote_count') or 0}",
        "",
        "## Post Text",
        _threads_post_text(root) or "(no text)",
        "",
    ]

    img_urls = _threads_image_urls(root)
    vid_urls = _threads_video_urls(root)
    lpa = tpa.get("link_preview_attachment") or {}
    share = tpa.get("share_info") or {}
    qp = share.get("quoted_post") or share.get("quoted_attachment_post")
    rp = share.get("reposted_post")
    snip_frags = (((tpa.get("snippet_attachment_info") or {}).get("text_fragments") or {}).get("fragments") or [])
    snip_text = "".join(str((f or {}).get("plaintext") or "") for f in snip_frags).strip()
    audio = (root.get("audio") or {}).get("audio_src")

    lines.append("## Attachments & Images")
    if img_urls:
        for i, u in enumerate(img_urls, 1):
            lines.append(f"- Image {i}: {u}")
    else:
        lines.append("- Images: none")

    has_att = False
    if vid_urls:
        has_att = True
        for i, u in enumerate(vid_urls, 1):
            lines.append(f"- Video {i}: {u}")
    if audio:
        has_att = True
        lines.append(f"- Audio: {audio}")
    if isinstance(lpa, dict) and (lpa.get("url") or lpa.get("title")):
        has_att = True
        lines.append(f"- Link: {lpa.get('title') or ''} ({lpa.get('url') or ''})".strip())
    if isinstance(qp, dict) and (qp.get("pk") or qp.get("code") or _threads_post_text(qp)):
        has_att = True
        qu = (qp.get("user") or {}).get("username") or "unknown"
        qcode = qp.get("code") or ""
        qpk = qp.get("pk") or ""
        qtxt = " ".join(_threads_post_text(qp).split())
        if len(qtxt) > 280:
            qtxt = qtxt[:280] + "…"
        lines.append(f"- Quoted Post: @{qu} (`{qcode}`, `id={qpk}`): {qtxt}")
    if isinstance(rp, dict) and (rp.get("pk") or rp.get("code") or _threads_post_text(rp)):
        has_att = True
        ru = (rp.get("user") or {}).get("username") or "unknown"
        rcode = rp.get("code") or ""
        rpk = rp.get("pk") or ""
        rtxt = " ".join(_threads_post_text(rp).split())[:280]
        lines.append(f"- Reposted Post: @{ru} (`{rcode}`, `id={rpk}`): {rtxt}")
    if snip_text:
        has_att = True
        preview = " ".join(snip_text.split())
        if len(preview) > 400:
            preview = preview[:400] + "…"
        lines.append(f"- Text Snippet ({len(snip_text)} chars): {preview}")
    if not has_att:
        lines.append("- Attachments: none")
    lines.append("")

    ordered_replies: list[tuple[str, str, int]] = []
    visited_walk: set[str] = {root_pk}

    def _walk_tree(parent_pk: str, depth: int) -> None:
        for cpk in children_map.get(parent_pk, []):
            if cpk in visited_walk:
                continue
            visited_walk.add(cpk)
            ordered_replies.append((cpk, parent_pk, depth))
            _walk_tree(cpk, depth + 1)

    _walk_tree(root_pk, 0)
    captured_count = len(ordered_replies)

    direct_only_set: set[str] = set()

    def _walk_direct_only(parent_pk: str) -> None:
        for cpk in direct_only_children_map.get(parent_pk, []):
            if cpk not in direct_only_set:
                direct_only_set.add(cpk)
                _walk_direct_only(cpk)

    _walk_direct_only(root_pk)
    direct_only_count = len(direct_only_set)

    unavailable_items: list[str] = list(unavailable_nodes)
    child_missing_sum = 0
    for cpk, _, _ in ordered_replies:
        cp = posts_by_pk[cpk]
        ctpa = cp.get("text_post_app_info") or {}
        cu = (cp.get("user") or {}).get("username") or "unknown"
        ccode = cp.get("code") or ""
        if ctpa.get("is_post_unavailable"):
            unavailable_items.append(f"reply `{ccode}` (`id={cpk}`, @{cu}): post_unavailable")
        drc = int(ctpa.get("direct_reply_count") or 0)
        got = len(children_map.get(cpk, []))
        got_direct = len(direct_only_children_map.get(cpk, []))
        if drc > got:
            diff = drc - got
            child_missing_sum += diff
            unavailable_items.append(
                f"under @{cu} (`{ccode}`, `id={cpk}`): {diff} unavailable reply (expected {drc}, captured {got})"
            )
        elif drc > got_direct:
            st_kids = [k for k in children_map.get(cpk, []) if k not in direct_only_children_map.get(cpk, [])]
            st_desc = ", ".join(f"`{posts_by_pk[k].get('code')}` (`id={k}`)" for k in st_kids)
            unavailable_items.append(
                f"under @{cu} (`{ccode}`, `id={cpk}`): {drc - got_direct} omitted from direct_replies "
                f"(expected {drc}, direct_replies=0; captured via self_thread: {st_desc})"
            )

    total_missing = max(0, thread_reply_count - captured_count)
    root_missing = max(0, total_missing - child_missing_sum)
    if root_missing > 0:
        unavailable_items.insert(
            0,
            f"under root @{uname} (`{code}`, `id={root_pk}`): {root_missing} unavailable direct reply "
            f"(has_unavailable_replies={bool(has_unavail_map.get(root_pk))})",
        )
    elif has_unavail_map.get(root_pk) and not unavailable_items:
        unavailable_items.append(
            f"root @{uname} (`{code}`, `id={root_pk}`): has_unavailable_replies=true"
        )

    lines.append("## Reply Summary")
    if direct_only_count != captured_count:
        lines.append(
            f"- Replies captured: {captured_count} / {thread_reply_count} "
            f"(direct_replies only: {direct_only_count} / {thread_reply_count})"
        )
    else:
        lines.append(f"- Replies captured: {captured_count} / {thread_reply_count}")
    lines.append(
        f"- Unavailable replies count: {total_missing}"
        + (f" (direct_replies missing: {max(0, thread_reply_count - direct_only_count)})"
           if direct_only_count != captured_count else "")
    )
    if unavailable_items:
        lines.append("- Unavailable replies:")
        for item in unavailable_items:
            lines.append(f"  - {item}")
    else:
        lines.append("- Unavailable replies: none")
    lines.append("")

    lines.append(f"## Replies ({captured_count})")
    if not ordered_replies:
        lines.append("(no replies)")
    else:
        for cpk, _, depth in ordered_replies:
            cp = posts_by_pk[cpk]
            cu = (cp.get("user") or {}).get("username") or "unknown"
            ccode = cp.get("code") or ""
            ctpa = cp.get("text_post_app_info") or {}
            ctxt = _threads_post_text(cp)
            ctxt_inline = " / ".join(line.strip() for line in ctxt.splitlines() if line.strip())
            cimgs = _threads_image_urls(cp)
            csnip_frags = (((ctpa.get("snippet_attachment_info") or {}).get("text_fragments") or {}).get("fragments") or [])
            csnip = "".join(str((f or {}).get("plaintext") or "") for f in csnip_frags).strip()
            extras: list[str] = []
            if cimgs:
                extras.append(f"images={len(cimgs)}")
            if csnip:
                extras.append(f"snippet={len(csnip)} chars")
            extra_str = (" [" + ", ".join(extras) + "]") if extras else ""
            indent = "  " * depth
            lines.append(f"{indent}- @{cu} [`{ccode}` | `id={cpk}`]: {ctxt_inline}{extra_str}")

    out = "\n".join(lines) + "\n"
    if len(out.encode("utf-8")) > _THREADS_MAX_MARKDOWN_BYTES:
        trimmed: list[str] = []
        used = 0
        for line in lines:
            b = len((line + "\n").encode("utf-8"))
            if used + b > _THREADS_MAX_MARKDOWN_BYTES - 120:
                trimmed.append("… (truncated to stay under 20 KB)")
                break
            trimmed.append(line)
            used += b
        out = "\n".join(trimmed) + "\n"
    return out


def _threads_legacy_inline_video(raw: str, code: str, final_url: str, status_code: int, attempts: list[dict]) -> dict:
    code_pos = [c.start() for c in re.finditer(r'"code"\s*:\s*"%s"' % re.escape(code), raw)]
    blocks = list(re.finditer(r'"video_versions"\s*:\s*\[(.*?)\]', raw))
    if not code_pos or not blocks:
        note = (f"status={status_code}" if status_code != 200
                else ("no-code-marker" if not code_pos else "no-threads-data"))
        attempts.append(_attempt("threads", "graphql", False, status_code, raw, note))
        return {"platform": "threads", "ok": False, "route": None, "content": "",
                "final_url": final_url, "attempts": attempts}
    best = min(blocks, key=lambda b: min(abs(b.start() - c) for c in code_pos))
    urls: list[str] = []
    for u in re.findall(r'"url"\s*:\s*"([^"]+)"', best.group(1)):
        u = u.replace("\\/", "/").encode().decode("unicode_escape")
        if u not in urls:
            urls.append(u)
    if not urls:
        attempts.append(_attempt("threads", "inline-json", False, status_code, raw, "empty-video_versions"))
        return {"platform": "threads", "ok": False, "route": None, "content": "",
                "final_url": final_url, "attempts": attempts}
    content = json.dumps({"post_code": code, "video_urls": urls}, ensure_ascii=False)
    attempts.append(_attempt("threads", "inline-json", True, status_code, content,
                             f"{len(urls)} video url(s)"))
    return {"platform": "threads", "ok": True, "route": "inline-json",
            "content": content, "final_url": final_url, "attempts": attempts}


def _threads(url: str, timeout: int) -> dict:
    """Threads post (/share/<code> or /@user/post/<code>) → structured Markdown
    with post metadata, attachments/images, and all nested replies via GraphQL
    (BarcelonaPostPageDownwardQuery, TOP and RECENT cursors, recursive child replies)."""
    attempts: list[dict] = []
    path_part = url.split("?", 1)[0]
    m_post = _THREADS_POST_RE.search(path_part)
    m_share = _THREADS_SHARE_RE.search(path_part)
    if not m_post and not m_share:
        attempts.append(_attempt("threads", "graphql", False, 0, "", "no-post-shortcode"))
        return {"platform": "threads", "ok": False, "route": None, "content": "",
                "final_url": url, "attempts": attempts}

    try:
        x = _cffi_get(url, timeout=timeout)
        resolved_url = str(getattr(x, "url", None) or url)
        if getattr(x, "status_code", 0) in (301, 302, 303, 307, 308):
            hdrs = getattr(x, "headers", None) or {}
            loc = hdrs.get("Location") or hdrs.get("location")
            if loc:
                resolved_url = urljoin(url, str(loc))
                x = _cffi_get(resolved_url, timeout=timeout)
                resolved_url = str(getattr(x, "url", None) or resolved_url)

        status_code = int(getattr(x, "status_code", 0) or 0)
        raw = x.text if status_code == 200 else ""
        if status_code != 200 or not raw:
            attempts.append(_attempt("threads", "graphql", False, status_code, raw, f"status={status_code}"))
            return {"platform": "threads", "ok": False, "route": None, "content": "",
                    "final_url": resolved_url, "attempts": attempts}

        cookies: dict[str, str] = {}
        raw_cookies = getattr(x, "cookies", None)
        if raw_cookies:
            try:
                cookies = dict(raw_cookies)
            except Exception:
                pass

        lsd, preloaders, relays = _parse_threads_html(raw)
        target_media: Optional[dict] = None
        downward_media: Optional[dict] = None
        for k, v in relays.items():
            if "TargetQuery" in k and isinstance(v.get("media"), dict):
                target_media = v["media"]
            elif "DownwardQuery" in k and isinstance(v.get("media"), dict):
                downward_media = v["media"]

        downward_doc_id = _THREADS_DOWNWARD_DOC_ID
        downward_vars = dict(_DEFAULT_THREADS_DOWNWARD_VARS)
        target_doc_id = _THREADS_TARGET_DOC_ID
        target_vars = dict(_DEFAULT_THREADS_DOWNWARD_VARS)
        for p in preloaders:
            qname = p.get("queryName")
            if qname == "BarcelonaPostPageDownwardQuery":
                if p.get("queryID"):
                    downward_doc_id = str(p["queryID"])
                if isinstance(p.get("variables"), dict):
                    downward_vars.update(p["variables"])
            elif qname == "BarcelonaPostPageTargetQuery":
                if p.get("queryID"):
                    target_doc_id = str(p["queryID"])
                if isinstance(p.get("variables"), dict):
                    target_vars.update(p["variables"])

        code = ""
        username = ""
        root_pk = ""
        if isinstance(target_media, dict):
            root_pk = str(target_media.get("pk") or "")
            code = str(target_media.get("code") or "")
            username = str((target_media.get("user") or {}).get("username") or "")

        if not code or not username:
            m_up = _THREADS_USER_POST_RE.search(resolved_url) or _THREADS_USER_POST_RE.search(html_lib.unescape(raw))
            if m_up:
                username = username or m_up.group(1)
                code = code or m_up.group(2)
            else:
                m_p2 = _THREADS_POST_RE.search(resolved_url.split("?", 1)[0]) or m_post
                if m_p2:
                    code = code or m_p2.group(1)

        if not root_pk:
            root_pk = str(downward_vars.get("postID") or target_vars.get("postID") or _shortcode_to_pk(code) or "")

        if username and code:
            final_url = f"https://www.threads.com/@{username}/post/{code}"
        elif _THREADS_POST_RE.search(resolved_url):
            final_url = resolved_url.split("?", 1)[0]
        else:
            final_url = resolved_url

        gql_calls = 0
        use_refetch = False

        def _gql_headers(friendly_name: str) -> dict[str, str]:
            hdrs = {
                "X-FB-Friendly-Name": friendly_name,
                "X-IG-App-ID": _THREADS_IG_APP_ID,
                "Origin": "https://www.threads.com",
                "Referer": final_url,
            }
            if lsd:
                hdrs["X-FB-LSD"] = lsd
            if cookies.get("csrftoken"):
                hdrs["X-CSRFToken"] = cookies["csrftoken"]
            return hdrs

        if target_media is None and root_pk:
            try:
                tvars = dict(target_vars, postID=str(root_pk))
                gql_calls += 1
                t_resp = _cffi_post(
                    _THREADS_GRAPHQL_URL,
                    data={
                        "lsd": lsd,
                        "fb_api_req_friendly_name": "BarcelonaPostPageTargetQuery",
                        "variables": json.dumps(tvars),
                        "doc_id": target_doc_id,
                    },
                    headers=_gql_headers("BarcelonaPostPageTargetQuery"),
                    cookies=cookies,
                    timeout=timeout,
                )
                if getattr(t_resp, "status_code", 0) == 200:
                    td = t_resp.json()
                    tmedia = ((td.get("data") or {}).get("media") if isinstance(td, dict) else None)
                    if isinstance(tmedia, dict) and tmedia.get("pk"):
                        target_media = tmedia
                        root_pk = str(tmedia.get("pk") or root_pk)
                        code = str(tmedia.get("code") or code)
                        username = str((tmedia.get("user") or {}).get("username") or username)
                        if username and code:
                            final_url = f"https://www.threads.com/@{username}/post/{code}"
            except Exception:
                pass

        if target_media is None or not root_pk:
            return _threads_legacy_inline_video(raw, code or (m_share.group(1) if m_share else ""), final_url, status_code, attempts)

        posts_by_pk: dict[str, dict] = {root_pk: target_media}
        children_map: dict[str, list[str]] = {}
        direct_only_children_map: dict[str, list[str]] = {}
        parent_of: dict[str, str] = {}
        has_unavail_map: dict[str, bool] = {}
        unavailable_nodes: list[str] = []
        needs_expansion: set[str] = set()

        def _call_downward(post_id: str, sort_order: str, after: Optional[str] = None) -> dict:
            nonlocal gql_calls, use_refetch
            v = dict(downward_vars, postID=str(post_id), sortOrder=sort_order)
            if after:
                v["after"] = after
            else:
                v.pop("after", None)

            queries = [
                ("BarcelonaPostPageDownwardQuery", downward_doc_id),
                ("BarcelonaPostPageDirectRepliesRefetchQuery", _THREADS_REFETCH_DOC_ID),
            ]
            if use_refetch:
                queries = [queries[1]]

            for qname, qid in queries:
                try:
                    gql_calls += 1
                    resp_g = _cffi_post(
                        _THREADS_GRAPHQL_URL,
                        data={
                            "lsd": lsd,
                            "fb_api_req_friendly_name": qname,
                            "variables": json.dumps(v),
                            "doc_id": qid,
                        },
                        headers=_gql_headers(qname),
                        cookies=cookies,
                        timeout=timeout,
                    )
                    if getattr(resp_g, "status_code", 0) == 200:
                        d = resp_g.json()
                        if isinstance(d, dict):
                            media_obj = (d.get("data") or {}).get("media")
                            if isinstance(media_obj, dict):
                                return media_obj
                            if d.get("errors"):
                                use_refetch = True
                except Exception:
                    continue

            # Tertiary SSR preloader fallback when /api/graphql is rate-limited and after is None
            if after is None and post_id in posts_by_pk:
                pnode = posts_by_pk[post_id]
                pu = (pnode.get("user") or {}).get("username")
                pcode = pnode.get("code")
                if pu and pcode:
                    ssr_url = f"https://www.threads.com/@{pu}/post/{pcode}"
                    if sort_order == "RECENT":
                        ssr_url += "?sort_order=recent"
                    try:
                        resp_ssr = _cffi_get(ssr_url, timeout=timeout)
                        if getattr(resp_ssr, "status_code", 0) == 200 and resp_ssr.text:
                            _, _, ssr_relays = _parse_threads_html(resp_ssr.text)
                            for rk, rv in ssr_relays.items():
                                if "DownwardQuery" in rk and isinstance(rv.get("media"), dict):
                                    return rv["media"]
                    except Exception:
                        pass
            return {}

        def _ingest_chain(parent_pk: str, p_edges: list[dict], *, from_self_thread: bool = False) -> Optional[str]:
            cur_parent = str(parent_pk)
            last_pk: Optional[str] = None
            for pe in p_edges:
                pnode = (pe or {}).get("node") or {}
                if not isinstance(pnode, dict):
                    continue
                pk = str(pnode.get("pk") or "")
                ptpa = pnode.get("text_post_app_info") or {}
                if not pk:
                    if ptpa.get("is_post_unavailable") or ptpa.get("post_unavailable_reason"):
                        unavailable_nodes.append(f"under `id={cur_parent}`: unavailable reply node")
                    continue
                if pk not in posts_by_pk:
                    posts_by_pk[pk] = pnode
                if pk != root_pk and pk not in parent_of:
                    parent_of[pk] = cur_parent
                    children_map.setdefault(cur_parent, [])
                    if pk not in children_map[cur_parent]:
                        children_map[cur_parent].append(pk)
                actual_parent = parent_of.get(pk, cur_parent)
                if not from_self_thread:
                    direct_only_children_map.setdefault(actual_parent, [])
                    if pk not in direct_only_children_map[actual_parent]:
                        direct_only_children_map[actual_parent].append(pk)
                cur_parent = pk
                last_pk = pk
            return last_pk

        def _ingest_media(parent_pk: str, media_obj: Optional[dict]) -> tuple[dict, int]:
            if not isinstance(media_obj, dict) or not media_obj:
                return {}, 0
            tpa = media_obj.get("text_post_app_info") or {}
            if tpa.get("has_unavailable_replies"):
                has_unavail_map[str(parent_pk)] = True
            st_edges = (((tpa.get("self_thread") or {}).get("posts") or {}).get("edges") or [])
            if st_edges:
                _ingest_chain(parent_pk, st_edges, from_self_thread=True)
            for edge in ((tpa.get("pinned_replies") or {}).get("edges") or []):
                node = (edge or {}).get("node") or {}
                _ingest_chain(parent_pk, (((node.get("posts") or {}).get("edges")) or []), from_self_thread=False)
            dr = tpa.get("direct_replies") or {}
            edges = dr.get("edges") or []
            for edge in edges:
                node = (edge or {}).get("node") or {}
                p_edges = (((node.get("posts") or {}).get("edges")) or [])
                last_pk = _ingest_chain(parent_pk, p_edges, from_self_thread=False)
                p_pi = ((node.get("posts") or {}).get("page_info") or {})
                if p_pi.get("has_next_page") and last_pk:
                    needs_expansion.add(last_pk)
            return (dr.get("page_info") or {}), len(edges)

        # 1. Root post downward replies: TOP and RECENT cursors
        pi_html: dict = {}
        if downward_media is not None:
            pi_html, _ = _ingest_media(root_pk, downward_media)
            after_top: Optional[str] = str(pi_html["end_cursor"]) if (pi_html.get("has_next_page") and pi_html.get("end_cursor")) else None
            seen_top: set[str] = {after_top} if after_top else set()
            while after_top:
                m = _call_downward(root_pk, "TOP", after=after_top)
                pi_next, cnt = _ingest_media(root_pk, m)
                nxt = str(pi_next.get("end_cursor") or "") if (pi_next.get("has_next_page") and cnt > 0) else ""
                if not nxt or nxt in seen_top:
                    break
                seen_top.add(nxt)
                after_top = nxt
        else:
            after_top = None
            seen_top = set()
            while True:
                m = _call_downward(root_pk, "TOP", after=after_top)
                pi_next, cnt = _ingest_media(root_pk, m)
                nxt = str(pi_next.get("end_cursor") or "") if (pi_next.get("has_next_page") and cnt > 0) else ""
                if not nxt or nxt in seen_top:
                    break
                seen_top.add(nxt)
                after_top = nxt

        after_rec: Optional[str] = None
        seen_rec: set[str] = set()
        while True:
            m = _call_downward(root_pk, "RECENT", after=after_rec)
            pi_next, cnt = _ingest_media(root_pk, m)
            nxt = str(pi_next.get("end_cursor") or "") if (pi_next.get("has_next_page") and cnt > 0) else ""
            if not nxt or nxt in seen_rec:
                break
            seen_rec.add(nxt)
            after_rec = nxt

        # 2. Recursive child replies
        queried_pks: set[str] = {root_pk}
        while True:
            candidates: list[str] = []
            for pk, pnode in list(posts_by_pk.items()):
                if pk in queried_pks:
                    continue
                ptpa = pnode.get("text_post_app_info") or {}
                drc = int(ptpa.get("direct_reply_count") or 0)
                got = len(children_map.get(pk, []))
                if drc > got or pk in needs_expansion:
                    candidates.append(pk)
            if not candidates:
                break
            for pk in candidates:
                queried_pks.add(pk)
                for sort_order in ("TOP", "RECENT"):
                    after_child: Optional[str] = None
                    seen_child: set[str] = set()
                    while True:
                        m = _call_downward(pk, sort_order, after=after_child)
                        pi_next, cnt = _ingest_media(pk, m)
                        nxt = str(pi_next.get("end_cursor") or "") if (pi_next.get("has_next_page") and cnt > 0) else ""
                        if not nxt or nxt in seen_child:
                            break
                        seen_child.add(nxt)
                        after_child = nxt
        content = _format_threads_markdown(
            root_pk,
            posts_by_pk,
            children_map,
            direct_only_children_map,
            has_unavail_map,
            unavailable_nodes,
            final_url,
        )
        captured_replies = len(posts_by_pk) - 1
        expected_replies = int((target_media.get("text_post_app_info") or {}).get("direct_reply_count") or 0)
        attempts.append(_attempt(
            "threads",
            "graphql",
            True,
            200,
            content,
            f"BarcelonaPostPageDownwardQuery calls={gql_calls} replies={captured_replies}/{expected_replies}",
        ))
        return {
            "platform": "threads",
            "ok": True,
            "route": "graphql",
            "content": content,
            "final_url": final_url,
            "attempts": attempts,
        }
    except Exception as e:
        attempts.append(_attempt("threads", "graphql", False, 0, "", f"{type(e).__name__}"))
        return {"platform": "threads", "ok": False, "route": None, "content": "",
                "final_url": url, "attempts": attempts}

_ROUTERS = {"reddit": _reddit, "x": _x, "youtube": _youtube, "threads": _threads}


# --- public entrypoint -------------------------------------------------------
def route(url: str, *, timeout: int = 15) -> Optional[dict]:
    platform = _detect(url)
    if platform is None:
        return None
    return _ROUTERS[platform](url, timeout)
