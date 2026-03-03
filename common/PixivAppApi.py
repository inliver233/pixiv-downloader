# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests

from common.RequestsProxyAdapter import mount_proxy_host_header_adapter

OAUTH_TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"
APP_API_BASE_URL = "https://app-api.pixiv.net"
USER_FOLLOWING_URL = APP_API_BASE_URL + "/v1/user/following"
USER_ILLUSTS_URL = APP_API_BASE_URL + "/v1/user/illusts"
ILLUST_DETAIL_URL = APP_API_BASE_URL + "/v1/illust/detail"
USER_DETAIL_URL = APP_API_BASE_URL + "/v1/user/detail"

# Pixiv App OAuth public client identifiers.
APP_CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
APP_CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"

# This is the well-known Pixiv Android hash secret used for X-Client-Hash.
HASH_SECRET = "28c1fdd170a5204386cb1313c7077b34f83e4aaf4aa829ce78c231e05b0bae2c"


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except Exception:
        return None


def _as_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _now_epoch() -> float:
    return time.time()


def _client_time() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _build_oauth_headers(*, user_agent: str) -> Dict[str, str]:
    client_time = _client_time()
    return {
        "User-Agent": user_agent,
        "Accept-Language": "en_US",
        "App-OS": "android",
        "App-OS-Version": "11",
        "App-Version": "5.0.234",
        "X-Client-Time": client_time,
        "X-Client-Hash": hashlib.md5((client_time + HASH_SECRET).encode("utf-8")).hexdigest(),
    }


def _unwrap_response(data: Any) -> Dict[str, Any]:
    if isinstance(data, dict) and isinstance(data.get("response"), dict):
        return data["response"]
    if isinstance(data, dict):
        return data
    raise ValueError("Invalid OAuth response shape")


def _backoff_sleep(*, base_wait_sec: float, attempt: int, max_wait_sec: float = 60.0, jitter_ratio: float = 0.15) -> None:
    """
    Exponential backoff with jitter.
    attempt is 1-based.
    """
    attempt_i = max(1, int(attempt))
    wait = float(base_wait_sec) * (2 ** (attempt_i - 1))
    wait = min(wait, float(max_wait_sec))
    if wait <= 0:
        return
    jitter = random.random() * wait * float(max(0.0, jitter_ratio))
    time.sleep(wait + jitter)


def _retry_after_seconds(headers: Dict[str, Any]) -> Optional[float]:
    try:
        raw = headers.get("Retry-After")
    except Exception:
        raw = None
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        # Usually a number of seconds.
        sec = float(s)
        if sec >= 0:
            return sec
    except Exception:
        return None
    return None


@dataclass(frozen=True)
class PixivOauthToken:
    access_token: str
    refresh_token: Optional[str]
    expires_in: int
    user_id: Optional[int]


class PixivAppApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: Optional[int] = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class PixivAppApiClient:
    """
    Minimal Pixiv App API client (refresh_token based).

    - Uses OAuth refresh_token to obtain short-lived access_token (cached in memory)
    - Supports proxies (requests' proxies dict) and SSL verification toggle
    - Provides helpers for:
      - v1/user/following (for "关注列表")
      - v1/user/illusts + v1/illust/detail (for URL indexing)
    """

    def __init__(
        self,
        *,
        refresh_token: str,
        proxies: Optional[Dict[str, str]] = None,
        verify_ssl: bool = True,
        timeout_sec: float = 30.0,
        user_agent: str = "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)",
        refresh_margin_sec: float = 60.0,
        on_refresh_token_rotated: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._refresh_token = (refresh_token or "").strip()
        if not self._refresh_token:
            raise ValueError("refresh_token is required")

        self._proxies = proxies
        self._verify_ssl = bool(verify_ssl)
        self._timeout_sec = float(timeout_sec)
        self._user_agent = str(user_agent or "").strip() or "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"
        self._refresh_margin_sec = max(0.0, float(refresh_margin_sec))

        self._session = requests.Session()
        # Some proxy pool frontends hang on CONNECT when Host equals target host:port.
        # Enforce proxy Host header to be proxy host:port for CONNECT (best-effort).
        mount_proxy_host_header_adapter(self._session, enabled=True)
        self._access_token: str = ""
        self._expires_at: float = 0.0
        self._user_id: Optional[int] = None
        self._on_refresh_token_rotated = on_refresh_token_rotated

    @property
    def user_id(self) -> Optional[int]:
        return self._user_id

    @property
    def refresh_token(self) -> str:
        return self._refresh_token

    def _is_access_token_valid(self) -> bool:
        if not self._access_token:
            return False
        return _now_epoch() < (float(self._expires_at) - float(self._refresh_margin_sec))

    def _oauth_refresh(self) -> PixivOauthToken:
        payload = {
            "client_id": APP_CLIENT_ID,
            "client_secret": APP_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "get_secure_url": "1",
            "include_policy": "1",
            "device_token": "pixiv",
        }

        headers = _build_oauth_headers(user_agent=self._user_agent)

        last_req_exc: Optional[BaseException] = None
        for attempt in range(1, 4):
            try:
                resp = self._session.post(
                    OAUTH_TOKEN_URL,
                    data=payload,
                    headers=headers,
                    proxies=self._proxies,
                    verify=self._verify_ssl,
                    timeout=self._timeout_sec,
                )
            except requests.RequestException as exc:
                last_req_exc = exc
                _backoff_sleep(base_wait_sec=2.0, attempt=attempt, max_wait_sec=20.0)
                continue

            status = int(getattr(resp, "status_code", -1))
            text = getattr(resp, "text", "") or ""

            if status in (429, 500, 502, 503, 504):
                _backoff_sleep(base_wait_sec=2.0, attempt=attempt, max_wait_sec=60.0)
                continue

            if status != 200:
                raise PixivAppApiError("OAuth refresh failed", status_code=status, body=text[:500])

            try:
                data = resp.json()
            except Exception as exc:
                raise PixivAppApiError("OAuth response is not JSON", status_code=status, body=text[:500]) from exc
            break
        else:
            if last_req_exc is not None:
                raise PixivAppApiError("OAuth refresh network error", status_code=None, body=str(last_req_exc)) from last_req_exc
            raise PixivAppApiError("OAuth refresh failed after retries", status_code=None)

        payload2 = _unwrap_response(data)
        access_token = _as_str(payload2.get("access_token"))
        expires_in = _as_int(payload2.get("expires_in"))
        rotated_refresh = _as_str(payload2.get("refresh_token"))

        user_id: Optional[int] = None
        user = payload2.get("user")
        if isinstance(user, dict):
            user_id = _as_int(user.get("id"))

        if not access_token or not expires_in or expires_in <= 0:
            raise PixivAppApiError("OAuth response missing access_token/expires_in", status_code=status, body=text[:500])

        return PixivOauthToken(
            access_token=access_token,
            refresh_token=rotated_refresh,
            expires_in=int(expires_in),
            user_id=user_id,
        )

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        if not force_refresh and self._is_access_token_valid():
            return self._access_token

        token = self._oauth_refresh()
        self._access_token = token.access_token
        self._expires_at = _now_epoch() + float(token.expires_in)
        if token.user_id is not None:
            self._user_id = int(token.user_id)
        if token.refresh_token and token.refresh_token != self._refresh_token:
            cb = self._on_refresh_token_rotated
            if cb is not None:
                try:
                    cb(str(token.refresh_token))
                except Exception:
                    pass
            self._refresh_token = token.refresh_token
        return self._access_token

    def _auth_headers(self, access_token: str) -> Dict[str, str]:
        headers = _build_oauth_headers(user_agent=self._user_agent)
        headers["Authorization"] = f"Bearer {access_token}"
        return headers

    def _get_json_with_retry(
        self,
        url: str,
        *,
        params: Optional[Dict[str, str]] = None,
        max_retries: int = 3,
        base_wait_sec: float = 2.0,
    ) -> Dict[str, Any]:
        last_exc: Optional[BaseException] = None
        access_token = self.get_access_token()

        for attempt in range(1, max(1, int(max_retries)) + 1):
            try:
                resp = self._session.get(
                    url,
                    params=params,
                    headers=self._auth_headers(access_token),
                    proxies=self._proxies,
                    verify=self._verify_ssl,
                    timeout=self._timeout_sec,
                    allow_redirects=True,
                )
            except requests.RequestException as exc:  # noqa: PERF203
                last_exc = exc
                _backoff_sleep(base_wait_sec=max(0.5, float(base_wait_sec)), attempt=attempt, max_wait_sec=30.0)
                continue

            status = int(getattr(resp, "status_code", -1))
            text = getattr(resp, "text", "") or ""

            if status == 401:
                # token expired/invalid; refresh once and retry
                access_token = self.get_access_token(force_refresh=True)
                _backoff_sleep(base_wait_sec=0.5, attempt=1, max_wait_sec=1.0, jitter_ratio=0.1)
                continue

            rate_limited = status == 429 or (status == 403 and "rate limit" in text.lower())
            if rate_limited:
                retry_after = _retry_after_seconds(getattr(resp, "headers", {}) or {})
                if retry_after is not None:
                    time.sleep(min(float(retry_after), 300.0))
                else:
                    _backoff_sleep(base_wait_sec=max(2.0, float(base_wait_sec)), attempt=attempt, max_wait_sec=300.0, jitter_ratio=0.2)
                continue

            if status in (500, 502, 503, 504):
                _backoff_sleep(base_wait_sec=max(1.0, float(base_wait_sec)), attempt=attempt, max_wait_sec=30.0)
                continue

            if status != 200:
                raise PixivAppApiError(f"App API request failed: HTTP {status}", status_code=status, body=text[:500])

            try:
                data = resp.json()
            except Exception as exc:
                raise PixivAppApiError("App API response is not JSON", status_code=status, body=text[:500]) from exc
            if not isinstance(data, dict):
                raise PixivAppApiError("App API response invalid", status_code=status, body=text[:200])
            return data

        if last_exc is not None:
            raise PixivAppApiError("Network error", status_code=None, body=str(last_exc)) from last_exc
        raise PixivAppApiError("Request failed after retries", status_code=None)

    @staticmethod
    def _get_next_url(payload: Dict[str, Any]) -> Optional[str]:
        nxt = payload.get("next_url")
        if isinstance(nxt, str) and nxt.strip():
            return nxt.strip()
        return None

    @staticmethod
    def _parse_next_params(next_url: str) -> Tuple[str, Dict[str, str]]:
        """
        App API returns next_url which already contains query params.
        We keep URL + params separated so requests can keep proxies/headers easily.
        """
        parsed = urlparse(next_url)
        q = parse_qs(parsed.query or "")
        params: Dict[str, str] = {}
        for k, v in q.items():
            if not k or not v:
                continue
            params[str(k)] = str(v[0])
        base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        return base, params

    @staticmethod
    def extract_following_user_ids(payload: Dict[str, Any]) -> Iterator[int]:
        previews = payload.get("user_previews")
        if isinstance(previews, list):
            for item in previews:
                if not isinstance(item, dict):
                    continue
                user = item.get("user")
                if not isinstance(user, dict):
                    continue
                uid = _as_int(user.get("id"))
                if uid is not None:
                    yield int(uid)

        # fallback shapes
        users = payload.get("users")
        if isinstance(users, list):
            for user in users:
                if not isinstance(user, dict):
                    continue
                uid = _as_int(user.get("id"))
                if uid is not None:
                    yield int(uid)

    def fetch_following_user_ids(
        self,
        *,
        user_id: int,
        restrict: str,
        page_sleep_sec: float = 0.5,
    ) -> List[int]:
        restrict = str(restrict or "").strip().lower()
        if restrict not in ("public", "private"):
            raise ValueError("restrict must be public/private")

        all_ids: List[int] = []
        seen: set[int] = set()

        url = USER_FOLLOWING_URL
        params: Dict[str, str] = {"user_id": str(int(user_id)), "restrict": restrict}
        page = 1

        while True:
            data = self._get_json_with_retry(url, params=params)
            added = 0
            for uid in self.extract_following_user_ids(data):
                if uid in seen:
                    continue
                seen.add(uid)
                all_ids.append(uid)
                added += 1

            nxt = self._get_next_url(data)
            if not nxt:
                break

            page += 1
            if float(page_sleep_sec) > 0:
                time.sleep(float(page_sleep_sec))
            url, params = self._parse_next_params(nxt)

        return all_ids

    def iter_user_illust_ids(
        self,
        *,
        user_id: int,
        types: Optional[Iterable[str]] = None,
        page_sleep_sec: float = 0.0,
    ) -> Iterator[int]:
        """
        Yield illust IDs for a user (best-effort).
        types: sequence of 'illust'/'manga' (default: both).
        """
        if types is None:
            types = ("illust", "manga")

        seen: set[int] = set()
        for t in types:
            t_norm = str(t or "").strip().lower()
            if t_norm not in ("illust", "manga"):
                continue

            url = USER_ILLUSTS_URL
            params: Dict[str, str] = {
                "user_id": str(int(user_id)),
                "type": t_norm,
                "filter": "for_android",
            }
            while True:
                data = self._get_json_with_retry(url, params=params)
                illusts = data.get("illusts")
                if isinstance(illusts, list):
                    for it in illusts:
                        if not isinstance(it, dict):
                            continue
                        iid = _as_int(it.get("id"))
                        if iid is None or iid <= 0:
                            continue
                        if iid in seen:
                            continue
                        seen.add(iid)
                        yield int(iid)

                nxt = self._get_next_url(data)
                if not nxt:
                    break
                if float(page_sleep_sec) > 0:
                    time.sleep(float(page_sleep_sec))
                url, params = self._parse_next_params(nxt)

    def fetch_illust_detail(self, *, illust_id: int) -> Dict[str, Any]:
        data = self._get_json_with_retry(
            ILLUST_DETAIL_URL,
            params={"illust_id": str(int(illust_id)), "filter": "for_android"},
        )
        return data

    def fetch_user_detail(self, *, user_id: int) -> Dict[str, Any]:
        data = self._get_json_with_retry(
            USER_DETAIL_URL,
            params={"user_id": str(int(user_id)), "filter": "for_android"},
        )
        return data

    @staticmethod
    def parse_user_detail(data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize user/detail response into a compact dict for DB upsert.
        Returns:
          {member_id, name, member_token, avatar_url, background_url}
        """
        user = data.get("user")
        profile = data.get("profile")
        if not isinstance(user, dict):
            payload = _unwrap_response(data)
            user = payload.get("user") if isinstance(payload, dict) else None
            profile = payload.get("profile") if isinstance(payload, dict) else None
        if not isinstance(user, dict):
            raise ValueError("Missing user in response")

        member_id = _as_int(user.get("id")) or 0
        name = _as_str(user.get("name")) or ""
        member_token = _as_str(user.get("account")) or ""

        avatar_url = ""
        prof_imgs = user.get("profile_image_urls")
        if isinstance(prof_imgs, dict):
            avatar_url = _as_str(prof_imgs.get("medium") or prof_imgs.get("px_170x170") or prof_imgs.get("square_medium")) or ""

        background_url = ""
        if isinstance(profile, dict):
            background_url = _as_str(profile.get("background_image_url")) or ""

        return {
            "member_id": int(member_id),
            "name": name,
            "member_token": member_token,
            "avatar_url": avatar_url,
            "background_url": background_url,
        }

    @staticmethod
    def _extract_original_urls(illust: Dict[str, Any], *, page_count: int) -> List[str]:
        urls: List[str] = []
        if page_count <= 1:
            meta_single = illust.get("meta_single_page")
            if isinstance(meta_single, dict):
                url = _as_str(meta_single.get("original_image_url"))
                if url:
                    return [url]
            meta_pages = illust.get("meta_pages")
            if isinstance(meta_pages, list) and meta_pages:
                page0 = meta_pages[0]
                if isinstance(page0, dict):
                    img_urls = page0.get("image_urls")
                    if isinstance(img_urls, dict):
                        url = _as_str(img_urls.get("original"))
                        if url:
                            return [url]
            return []

        meta_pages = illust.get("meta_pages")
        if isinstance(meta_pages, list):
            for page in meta_pages:
                if not isinstance(page, dict):
                    continue
                img_urls = page.get("image_urls")
                if not isinstance(img_urls, dict):
                    continue
                url = _as_str(img_urls.get("original"))
                if url:
                    urls.append(url)
        return urls

    @staticmethod
    def _extract_regular_urls(illust: Dict[str, Any], *, page_count: int) -> List[str]:
        urls: List[str] = []
        if page_count <= 1:
            image_urls = illust.get("image_urls")
            if isinstance(image_urls, dict):
                url = _as_str(image_urls.get("large") or image_urls.get("medium") or image_urls.get("square_medium"))
                if url:
                    return [url]
            meta_single = illust.get("meta_single_page")
            if isinstance(meta_single, dict):
                url = _as_str(meta_single.get("large_image_url"))
                if url:
                    return [url]
            return []

        meta_pages = illust.get("meta_pages")
        if isinstance(meta_pages, list):
            for page in meta_pages:
                if not isinstance(page, dict):
                    continue
                img_urls = page.get("image_urls")
                if not isinstance(img_urls, dict):
                    continue
                url = _as_str(img_urls.get("large") or img_urls.get("medium") or img_urls.get("square_medium"))
                if url:
                    urls.append(url)
        return urls

    @staticmethod
    def parse_illust_detail(data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize illust/detail response into a compact dict for DB upsert.
        Returns:
          {
            image_id, title, caption, create_date, page_count, mode,
            bookmark_count, view_count,
            url_rows: [(image_id, page_index, original_url, regular_url), ...]
          }
        """
        illust = data.get("illust")
        if not isinstance(illust, dict):
            # sometimes wrapped
            payload = _unwrap_response(data)
            illust = payload.get("illust") if isinstance(payload, dict) else None
        if not isinstance(illust, dict):
            raise ValueError("Missing illust in response")

        image_id = _as_int(illust.get("id")) or 0
        page_count = _as_int(illust.get("page_count")) or 1
        title = _as_str(illust.get("title")) or ""
        caption = _as_str(illust.get("caption")) or ""
        create_date = _as_str(illust.get("create_date")) or ""

        illust_type = _as_int(illust.get("illust_type"))
        mode = "illust"
        if illust_type == 1:
            mode = "manga"
        elif illust_type == 2:
            mode = "ugoira_view"

        bookmark_count = _as_int(illust.get("total_bookmarks"))
        view_count = _as_int(illust.get("total_view"))

        originals = PixivAppApiClient._extract_original_urls(illust, page_count=page_count)
        regulars = PixivAppApiClient._extract_regular_urls(illust, page_count=page_count)

        url_rows: List[Tuple[int, int, str, str]] = []
        for page_index, ori_url in enumerate(originals):
            reg_url = regulars[page_index] if page_index < len(regulars) else ""
            url_rows.append((int(image_id), int(page_index), str(ori_url), str(reg_url)))

        return {
            "image_id": int(image_id),
            "title": title,
            "caption": caption,
            "create_date": create_date,
            "page_count": int(page_count),
            "mode": mode,
            "bookmark_count": bookmark_count,
            "view_count": view_count,
            "url_rows": url_rows,
        }
