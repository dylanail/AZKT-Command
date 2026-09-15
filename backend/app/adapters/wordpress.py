"""WordPress / WooCommerce website adapter (spec §7.2, §12.3).

Capabilities: `discover`, `read_existing`, `validate_package`, `preview`, `upsert_draft`, `publish`,
`update_availability`, `read_back`, `archive`. Anything the installed site does not expose returns
`Unsupported` with a typed reason — never a fake success.

Two authentications with different capabilities are kept apart:

* `WordPressAdapter` — WP REST (`/wp-json/wp/v2/...`) with an application password. Media, posts and
  custom post types.
* `WooAdapter` — WooCommerce REST (`/wp-json/wc/v3/...`) with consumer key/secret. Products, stock and
  catalog visibility.

`WebsiteAdapter` is the facade the listings service talks to; it routes each operation to the API that
owns it according to the active site profile (`content_type` product | post | custom).

`FakeWordPress` implements the same facade in memory with the fixtures the acceptance scenarios need:
an existing product for a vehicle with no local mapping (F05), a manual price edit by a human editor
(F08), a public page whose cache lags behind the API (F07) and a network failure *after* the site
accepted a write (F06).
"""
from __future__ import annotations

import base64
import re
from typing import Any

from ..core.destinations import assert_destination_allowed
from ..core.errors import ProviderError, Unsupported

WP_NAMESPACE = "wp/v2"
WC_NAMESPACE = "wc/v3"
OPERATIONS = ("discover", "read_existing", "validate_package", "preview", "upsert_draft", "publish",
              "update_availability", "read_back", "archive")
VEHICLE_TYPE_HINTS = ("vehicle", "truck", "kei", "inventory", "listing", "car")

# Fields AZKT owns on a managed listing. Everything else belongs to the site's editors and is
# preserved untouched on every write (spec §7.2).
AZKT_FIELDS = ("title", "body", "short_description", "price", "sku", "media", "availability", "visibility",
               "disclosures", "specs")


def error_kind(exc: Exception) -> str:
    if isinstance(exc, ProviderError):
        return str((exc.detail or {}).get("kind") or "unknown_result")
    return "unknown_result"


def _fail(kind: str, message: str, **extra) -> ProviderError:
    return ProviderError(message, kind=kind, provider="wordpress", **extra)


class UnknownWriteResult(Exception):
    """The site may have accepted the write but the result was lost (network failure after accept)."""

    def __init__(self, message: str, provider_ref: str | None = None):
        super().__init__(message)
        self.provider_ref = provider_ref


DEFAULT_PROFILE_FIELD_MAP = {
    "product": {"title": "name", "body": "description", "short_description": "short_description",
                "price": "regular_price", "sku": "sku", "media": "images", "availability": "stock_status",
                "visibility": "catalog_visibility", "status": "status"},
    "post": {"title": "title", "body": "content", "short_description": "excerpt", "price": "meta.vehicle_price",
             "sku": "meta.stock_no", "media": "meta.gallery", "availability": "meta.availability",
             "visibility": "status", "status": "status"},
}
DEFAULT_AVAILABILITY_MAP = {
    "product": {"available": {"stock_status": "instock", "catalog_visibility": "visible", "purchasable": False},
                "reserved": {"stock_status": "outofstock", "catalog_visibility": "visible", "purchasable": False},
                "sold": {"stock_status": "outofstock", "catalog_visibility": "hidden", "purchasable": False},
                "en_route": {"stock_status": "onbackorder", "catalog_visibility": "visible", "purchasable": False}},
    "post": {"available": {"meta": {"availability": "available"}, "status": "publish"},
             "reserved": {"meta": {"availability": "reserved"}, "status": "publish"},
             "sold": {"meta": {"availability": "sold"}, "status": "private"},
             "en_route": {"meta": {"availability": "en_route"}, "status": "publish"}},
}


def content_shape(profile: dict | None) -> str:
    """WooCommerce products have their own field set; every other managed type (a post or a custom
    vehicle post type) is written through the WP post fields + meta."""
    return "product" if ((profile or {}).get("content_type") or "product") == "product" else "post"


def field_map_for(profile: dict | None) -> dict:
    base = dict(DEFAULT_PROFILE_FIELD_MAP[content_shape(profile)])
    base.update({k: v for k, v in ((profile or {}).get("field_map") or {}).items() if v})
    return base


def availability_map_for(profile: dict | None) -> dict:
    base = {k: dict(v) for k, v in DEFAULT_AVAILABILITY_MAP[content_shape(profile)].items()}
    for k, v in ((profile or {}).get("availability_map") or {}).items():
        base[k] = {**base.get(k, {}), **(v or {})}
    return base


def render_payload(package: dict, profile: dict | None) -> dict:
    """Deterministic mapping of a listing package onto the site's schema. Pure: never calls the site."""
    fmap = field_map_for(profile)
    out: dict[str, Any] = {}
    meta: dict[str, Any] = {}

    def put(field: str, value) -> None:
        target = fmap.get(field)
        if not target or value is None:
            return
        if target.startswith("meta."):
            meta[target.split(".", 1)[1]] = value
        else:
            out[target] = value

    put("title", package.get("headline"))
    put("body", package.get("body"))
    put("short_description", package.get("short_description"))
    if package.get("price") is not None:
        put("price", str(package["price"]))
    put("sku", package.get("sku"))
    media = [{"src": m.get("url"), "alt": m.get("alt") or package.get("headline"), "sha256": m.get("sha256"),
              "position": i} for i, m in enumerate(package.get("media") or [])]
    if media:
        put("media", media)
    avail = availability_map_for(profile).get(package.get("availability") or "available", {})
    for k, v in avail.items():
        if k == "meta":
            meta.update(v or {})
        else:
            out[k] = v
    if package.get("disclosures"):
        meta["azkt_disclosures"] = list(package["disclosures"])
    if package.get("specs"):
        meta["azkt_specs"] = list(package["specs"])
    meta["azkt_package_hash"] = package.get("package_hash")
    meta["azkt_vehicle_id"] = package.get("vehicle_id")
    if meta:
        out["meta_data" if content_shape(profile) == "product" else "meta"] = meta
    out["status"] = package.get("status") or "draft"
    return out


def validate_package(package: dict, profile: dict | None) -> dict:
    """Schema-level validation against the discovered profile. Returns {ok, errors[], warnings[]}."""
    errors: list[str] = []
    warnings: list[str] = []
    required = list((profile or {}).get("validation", {}).get("required_fields") or ["headline", "body"])
    for f in required:
        if not package.get(f):
            errors.append(f"required field {f} is missing")
    if package.get("price") is None:
        errors.append("no approved price")
    payload = render_payload(package, profile)
    unknown = [f for f in ((profile or {}).get("validation", {}).get("unknown_required_fields") or [])]
    for f in unknown:
        errors.append(f"site requires an unknown field {f}; re-discover the profile before writing")
    media_rules = (profile or {}).get("media_rules") or {}
    if media_rules.get("min") and len(package.get("media") or []) < int(media_rules["min"]):
        errors.append(f"needs at least {media_rules['min']} images")
    if not package.get("media"):
        warnings.append("no images in the package")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "payload": payload}


class _HttpBase:
    def __init__(self, base_url: str, *, timeout: float = 30.0):
        if not base_url:
            raise Unsupported("website base URL is not configured (setup blocked)")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _auth_headers(self) -> dict:
        return {}

    def _auth_params(self) -> dict:
        return {}

    async def request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> Any:
        import httpx
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as c:
                r = await c.request(method, url, params={**(params or {}), **self._auth_params()} or None,
                                    json=json, headers={"Accept": "application/json", **self._auth_headers()})
        except httpx.TimeoutException as e:
            if method != "GET":
                # the site may have accepted the write: never retried blindly (F06)
                raise UnknownWriteResult(f"website timeout on {method} {path}") from e
            raise _fail("transient", f"website timeout: {e}") from e
        except httpx.HTTPError as e:
            if method != "GET":
                raise UnknownWriteResult(f"website transport error on {method} {path}: {e}") from e
            raise _fail("transient", f"website transport error: {e}") from e
        if r.status_code == 401:
            raise _fail("auth_expired", "website rejected the integration credentials (401)")
        if r.status_code == 403:
            raise _fail("permission_denied", "website credentials lack the required capability (403)")
        if r.status_code == 404:
            raise _fail("invalid_input", f"website route not found: {path}")
        if r.status_code == 409:
            raise _fail("conflict", "website reported a conflict (409)")
        if r.status_code == 429:
            raise _fail("rate_limited", "website rate limited (429)")
        if r.status_code >= 500:
            if method != "GET":
                raise UnknownWriteResult(f"website returned {r.status_code} after accepting {method} {path}")
            raise _fail("transient", f"website error {r.status_code}")
        if r.status_code >= 400:
            raise _fail("invalid_input", f"website rejected the request ({r.status_code}): {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise _fail("schema_changed", "website returned a non-JSON body where JSON was expected") from e


class WordPressAdapter(_HttpBase):
    """WP REST with an application password (media, posts, custom post types)."""

    kind = "live"

    def __init__(self, base_url: str, app_user: str = "", app_password: str = "", **kw):
        super().__init__(base_url, **kw)
        self.app_user, self.app_password = app_user, app_password

    def _auth_headers(self) -> dict:
        if not (self.app_user and self.app_password):
            return {}
        token = base64.b64encode(f"{self.app_user}:{self.app_password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    async def index(self) -> dict:
        return await self.request("GET", "/wp-json")

    async def types(self) -> dict:
        return await self.request("GET", f"/wp-json/{WP_NAMESPACE}/types")

    async def me(self) -> dict:
        return await self.request("GET", f"/wp-json/{WP_NAMESPACE}/users/me", params={"context": "edit"})

    async def media_capabilities(self) -> dict:
        try:
            await self.request("GET", f"/wp-json/{WP_NAMESPACE}/media", params={"per_page": 1})
            return {"read": True, "upload": bool(self.app_user and self.app_password)}
        except ProviderError as e:
            return {"read": False, "upload": False, "error": error_kind(e)}

    async def posts(self, post_type: str = "posts", **params) -> list[dict]:
        rows = await self.request("GET", f"/wp-json/{WP_NAMESPACE}/{post_type}", params=params)
        return rows if isinstance(rows, list) else [rows]

    async def upsert_post(self, post_type: str, payload: dict, post_id: str | None = None) -> dict:
        path = f"/wp-json/{WP_NAMESPACE}/{post_type}" + (f"/{post_id}" if post_id else "")
        return await self.request("POST", path, json=payload)


class WooAdapter(_HttpBase):
    """WooCommerce REST with consumer key/secret (products, stock, visibility)."""

    kind = "live"

    def __init__(self, base_url: str, consumer_key: str = "", consumer_secret: str = "", **kw):
        super().__init__(base_url, **kw)
        self.consumer_key, self.consumer_secret = consumer_key, consumer_secret

    def _auth_params(self) -> dict:
        if not (self.consumer_key and self.consumer_secret):
            return {}
        return {"consumer_key": self.consumer_key, "consumer_secret": self.consumer_secret}

    async def system_status(self) -> dict:
        return await self.request("GET", f"/wp-json/{WC_NAMESPACE}/system_status")

    async def attributes(self) -> list[dict]:
        rows = await self.request("GET", f"/wp-json/{WC_NAMESPACE}/products/attributes")
        return rows if isinstance(rows, list) else []

    async def categories(self) -> list[dict]:
        rows = await self.request("GET", f"/wp-json/{WC_NAMESPACE}/products/categories")
        return rows if isinstance(rows, list) else []

    async def products(self, **params) -> list[dict]:
        rows = await self.request("GET", f"/wp-json/{WC_NAMESPACE}/products", params=params)
        return rows if isinstance(rows, list) else [rows]

    async def product(self, product_id: str) -> dict:
        return await self.request("GET", f"/wp-json/{WC_NAMESPACE}/products/{product_id}")

    async def upsert_product(self, payload: dict, product_id: str | None = None) -> dict:
        path = f"/wp-json/{WC_NAMESPACE}/products" + (f"/{product_id}" if product_id else "")
        return await self.request("POST", path, json=payload)


class WebsiteAdapter:
    """Facade used by services/listings.py. Routes each operation to the API that owns it."""

    kind = "live"

    def __init__(self, wp: WordPressAdapter | None = None, woo: WooAdapter | None = None, *, base_url: str = "",
                 public_base_url: str | None = None):
        self.wp, self.woo = wp, woo
        self.base_url = (base_url or (wp.base_url if wp else "") or (woo.base_url if woo else "")).rstrip("/")
        self.public_base_url = (public_base_url or self.base_url).rstrip("/")

    # ── discovery ────────────────────────────────────────────────────────
    async def discover(self) -> dict:
        if self.wp is None:
            raise Unsupported("WordPress REST credentials are not configured (setup blocked)")
        index = await self.wp.index()
        namespaces = list(index.get("namespaces") or [])
        out: dict[str, Any] = {"site": {"name": index.get("name"), "url": index.get("url") or self.base_url,
                                        "description": index.get("description")},
                               "namespaces": namespaces, "wp_version": (index.get("_links") and "unknown") or "unknown",
                               "woocommerce": {"present": WC_NAMESPACE in namespaces}, "auth": {}, "post_types": {},
                               "limitations": []}
        try:
            me = await self.wp.me()
            out["auth"]["wordpress"] = {"user": me.get("slug") or me.get("name"),
                                        "capabilities": sorted([k for k, v in (me.get("capabilities") or {}).items() if v])[:60],
                                        "can_publish": bool((me.get("capabilities") or {}).get("publish_posts"))}
        except ProviderError as e:
            out["auth"]["wordpress"] = {"error": error_kind(e), "message": str(e)}
            out["limitations"].append(f"WordPress identity could not be read ({error_kind(e)})")
        types = {}
        try:
            types = await self.wp.types()
        except ProviderError as e:
            out["limitations"].append(f"post types could not be read ({error_kind(e)})")
        vehicle_types = []
        for slug, t in (types or {}).items():
            entry = {"slug": slug, "name": t.get("name"), "rest_base": t.get("rest_base"),
                     "taxonomies": list(t.get("taxonomies") or []), "supports": list((t.get("supports") or {}).keys())}
            out["post_types"][slug] = entry
            if slug == "product":
                continue
            if any(h in f"{slug} {t.get('name','')}".lower() for h in VEHICLE_TYPE_HINTS):
                vehicle_types.append(entry)
        out["vehicle_post_types"] = vehicle_types
        out["media"] = await self.wp.media_capabilities()
        if self.woo is not None and WC_NAMESPACE in namespaces:
            try:
                status = await self.woo.system_status()
                env = status.get("environment") or {}
                out["woocommerce"] = {"present": True, "version": env.get("version") or status.get("version"),
                                      "currency": (status.get("settings") or {}).get("currency"),
                                      "api_enabled": bool((status.get("settings") or {}).get("api_enabled", True))}
                out["auth"]["woocommerce"] = {"read": True, "write": True}
            except ProviderError as e:
                out["woocommerce"] = {"present": True, "error": error_kind(e)}
                out["auth"]["woocommerce"] = {"read": False, "write": False, "error": error_kind(e)}
                out["limitations"].append(f"WooCommerce system status is not readable ({error_kind(e)}); "
                                          "product writes are not proven")
            try:
                out["product_attributes"] = [{"id": a.get("id"), "name": a.get("name"), "slug": a.get("slug")}
                                             for a in await self.woo.attributes()]
                out["product_categories"] = [{"id": c.get("id"), "name": c.get("name"), "slug": c.get("slug")}
                                             for c in await self.woo.categories()]
            except ProviderError as e:
                out["limitations"].append(f"product taxonomy is not readable ({error_kind(e)})")
        else:
            out["limitations"].append("WooCommerce REST is not available on this site")
        # the managed content type is a discovered fact, never an assumption
        if out["woocommerce"].get("present") and not vehicle_types:
            out["content_type"] = "product"
        elif vehicle_types:
            out["content_type"] = "custom"
            out["custom_post_type"] = vehicle_types[0]["rest_base"] or vehicle_types[0]["slug"]
        else:
            out["content_type"] = "post"
        out["supported_ops"] = list(OPERATIONS)
        return out

    # ── reads ────────────────────────────────────────────────────────────
    async def read_existing(self, *, sku: str | None = None, title: str | None = None,
                            external_id: str | None = None, profile: dict | None = None) -> list[dict]:
        if content_shape(profile) == "product":
            if self.woo is None:
                raise Unsupported("WooCommerce credentials are not configured")
            if external_id:
                return [_normalize_product(await self.woo.product(external_id))]
            params = {"sku": sku} if sku else ({"search": title} if title else {})
            return [_normalize_product(p) for p in await self.woo.products(**params)]
        if self.wp is None:
            raise Unsupported("WordPress credentials are not configured")
        rest_base = (profile or {}).get("rest_base") or "posts"
        if external_id:
            return [_normalize_post(p) for p in await self.wp.posts(f"{rest_base}/{external_id}")]
        return [_normalize_post(p) for p in await self.wp.posts(rest_base, search=title or sku or "")]

    def validate_package(self, package: dict, profile: dict | None) -> dict:
        return validate_package(package, profile)

    async def preview(self, package: dict, profile: dict | None) -> dict:
        """Render the mapped payload. No write, no side effect (invariant 11)."""
        res = validate_package(package, profile)
        return {"ok": res["ok"], "payload": res["payload"], "errors": res["errors"], "warnings": res["warnings"],
                "target": (profile or {}).get("staging_url") or self.base_url, "written": False}

    # ── writes ───────────────────────────────────────────────────────────
    def _assert_writable(self, op: str) -> None:
        """H08: this adapter really delivers, so outside production it may only write to an
        allowlisted site. Every write goes through here — listing media travel inside these payloads
        as URLs, so there is no separate upload path to leave unguarded. `FakeWordPress` never comes
        here: nothing leaves the process, exactly like the memory email transport."""
        targets = {self.base_url, self.public_base_url} - {""}
        assert_destination_allowed("site", *sorted(targets))

    async def upsert_draft(self, package: dict, profile: dict | None, external_id: str | None = None) -> dict:
        self._assert_writable("upsert_draft")
        payload = {**render_payload(package, profile), "status": "draft"}
        if content_shape(profile) == "product":
            if self.woo is None:
                raise Unsupported("WooCommerce credentials are not configured")
            row = await self.woo.upsert_product(payload, external_id)
            norm = _normalize_product(row)
        else:
            if self.wp is None:
                raise Unsupported("WordPress credentials are not configured")
            rest_base = (profile or {}).get("rest_base") or "posts"
            row = await self.wp.upsert_post(rest_base, payload, external_id)
            norm = _normalize_post(row)
        return {"state": "accepted", "external_id": norm["external_id"], "external_url": norm.get("url"),
                "status": norm.get("status"), "payload": payload}

    async def publish(self, external_id: str, package: dict, profile: dict | None) -> dict:
        self._assert_writable("publish")
        payload = {**render_payload(package, profile), "status": "publish"}
        if content_shape(profile) == "product":
            row = await self.woo.upsert_product(payload, external_id)
            norm = _normalize_product(row)
        else:
            rest_base = (profile or {}).get("rest_base") or "posts"
            row = await self.wp.upsert_post(rest_base, payload, external_id)
            norm = _normalize_post(row)
        return {"state": "published", "external_id": norm["external_id"], "external_url": norm.get("url"),
                "status": norm.get("status")}

    async def update_availability(self, external_id: str, availability: str, profile: dict | None) -> dict:
        self._assert_writable("update_availability")
        mapping = availability_map_for(profile).get(availability)
        if mapping is None:
            raise Unsupported(f"the active site profile has no availability mapping for {availability!r}")
        payload = dict(mapping)
        if content_shape(profile) == "product":
            payload.pop("purchasable", None)   # read-only in Woo; visibility/stock carry the meaning
            row = await self.woo.upsert_product(payload, external_id)
            norm = _normalize_product(row)
        else:
            rest_base = (profile or {}).get("rest_base") or "posts"
            row = await self.wp.upsert_post(rest_base, payload, external_id)
            norm = _normalize_post(row)
        return {"state": "accepted", "external_id": norm["external_id"], "availability": availability,
                "observed": norm, "payload": payload}

    async def read_back(self, external_id: str, *, profile: dict | None = None, cache_bust: bool = True) -> dict:
        if content_shape(profile) == "product":
            api = _normalize_product(await self.woo.product(external_id))
        else:
            rest_base = (profile or {}).get("rest_base") or "posts"
            api = _normalize_post((await self.wp.posts(f"{rest_base}/{external_id}"))[0])
        public: dict = {"fetched": False}
        url = api.get("url")
        if url:
            import httpx
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
                    r = await c.get(url, params={"azkt_cb": "1"} if cache_bust else None,
                                    headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})
                public = {"fetched": r.status_code == 200, "status_code": r.status_code,
                          "html": r.text[:200000] if r.status_code == 200 else "",
                          "cache": r.headers.get("x-cache") or r.headers.get("cf-cache-status")}
            except Exception as e:  # noqa: BLE001 - a cache/CDN failure is not a write failure
                public = {"fetched": False, "error": str(e)[:200]}
        return {"api": api, "public": public}

    async def archive(self, external_id: str, profile: dict | None = None) -> dict:
        self._assert_writable("archive")
        payload = ({"status": "draft", "catalog_visibility": "hidden"} if content_shape(profile) == "product"
                   else {"status": "draft"})
        if content_shape(profile) == "product":
            norm = _normalize_product(await self.woo.upsert_product(payload, external_id))
        else:
            rest_base = (profile or {}).get("rest_base") or "posts"
            norm = _normalize_post(await self.wp.upsert_post(rest_base, payload, external_id))
        return {"state": "unpublished", "external_id": norm["external_id"], "status": norm.get("status")}

    # explicitly unsupported (spec §7.2: never change sitewide configuration)
    async def install_plugin(self, *_a, **_kw):
        raise Unsupported("AZKT never installs plugins or changes theme/plugin settings")

    async def update_site_settings(self, *_a, **_kw):
        raise Unsupported("payment/tax/shipping settings are read for context only and never changed")


def _normalize_product(p: dict) -> dict:
    return {"external_id": str(p.get("id")), "kind": "product", "sku": p.get("sku"), "title": p.get("name"),
            "status": p.get("status"), "price": p.get("regular_price"), "sale_price": p.get("sale_price"),
            "stock_status": p.get("stock_status"), "visibility": p.get("catalog_visibility"),
            "url": p.get("permalink"), "body": p.get("description"), "short_description": p.get("short_description"),
            "images": [{"id": str(i.get("id")), "src": i.get("src"), "alt": i.get("alt")} for i in (p.get("images") or [])],
            "meta": {m.get("key"): m.get("value") for m in (p.get("meta_data") or []) if isinstance(m, dict)},
            "modified": p.get("date_modified_gmt") or p.get("date_modified"), "raw": p}


def _normalize_post(p: dict) -> dict:
    def _r(v):
        return v.get("rendered") if isinstance(v, dict) else v
    meta = dict(p.get("meta") or {})
    return {"external_id": str(p.get("id")), "kind": "post", "sku": meta.get("stock_no"), "title": _r(p.get("title")),
            "status": p.get("status"), "price": meta.get("vehicle_price"), "stock_status": meta.get("availability"),
            "visibility": p.get("status"), "url": p.get("link"), "body": _r(p.get("content")),
            "short_description": _r(p.get("excerpt")), "images": list(meta.get("gallery") or []),
            "meta": meta, "modified": p.get("modified_gmt") or p.get("modified"), "raw": p}


# ── fake ─────────────────────────────────────────────────────────────────────
class FakeWordPress:
    """In-memory site used by tests. Same facade as `WebsiteAdapter`."""

    kind = "fake"

    def __init__(self, base_url: str = "https://azkeitrucks.example", *, content_type: str = "product"):
        self.base_url = base_url.rstrip("/")
        self.public_base_url = self.base_url
        self.content_type = content_type
        self.items: dict[str, dict] = {}
        self.public: dict[str, dict] = {}        # rendered page state (may lag the API: F07)
        self.calls: list[tuple[str, str]] = []
        self.next_id = 100
        self.namespaces = ["oembed/1.0", WP_NAMESPACE, WC_NAMESPACE]
        self.custom_types: dict[str, dict] = {}
        self.wp_capabilities = {"edit_posts": True, "publish_posts": True, "upload_files": True}
        self.woo_auth_ok = True
        self.media_upload = True
        self.fail_after_accept = False           # accept the write then lose the response (F06)
        self.fail_public_fetch = False
        self.public_cache_lag = False            # public page keeps the previous values (F07)
        self.unsupported_ops: set[str] = set()
        self.settings_changes = 0

    # fixtures -----------------------------------------------------------
    def add_product(self, *, sku: str | None, name: str, price: str, status: str = "publish",
                    external_id: str | None = None, meta: dict | None = None, images: list | None = None) -> dict:
        pid = external_id or str(self.next_id)
        self.next_id = max(self.next_id + 1, int(pid) + 1 if pid.isdigit() else self.next_id + 1)
        row = {"id": pid, "sku": sku, "name": name, "regular_price": price, "status": status,
               "stock_status": "instock", "catalog_visibility": "visible",
               "permalink": f"{self.base_url}/product/{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')}",
               "description": "", "short_description": "", "images": images or [],
               "meta_data": [{"key": k, "value": v} for k, v in (meta or {}).items()],
               "date_modified_gmt": "2026-09-01T00:00:00", "_edited_by": None}
        self.items[pid] = row
        self.public[pid] = dict(row)
        return row

    def add_custom_type(self, slug: str, name: str, rest_base: str | None = None) -> None:
        self.custom_types[slug] = {"name": name, "rest_base": rest_base or slug, "slug": slug,
                                   "taxonomies": ["vehicle_make"], "supports": {"title": True, "custom-fields": True}}

    def manual_edit(self, external_id: str, field: str, value, *, editor: str = "human") -> None:
        """A person edited a managed field in wp-admin (F08)."""
        row = self.items[external_id]
        row[field] = value
        row["_edited_by"] = editor
        row["date_modified_gmt"] = "2026-09-15T12:00:00"
        if not self.public_cache_lag:
            self.public[external_id] = dict(row)

    def set_public_stale(self, external_id: str, field: str, value) -> None:
        page = dict(self.public.get(external_id) or self.items.get(external_id) or {})
        page[field] = value
        self.public[external_id] = page
        self.public_cache_lag = True

    # facade -------------------------------------------------------------
    def _guard(self, op: str) -> None:
        self.calls.append((op, ""))
        if op in self.unsupported_ops:
            raise Unsupported(f"{op} is not supported by this site profile")

    async def discover(self) -> dict:
        self._guard("discover")
        vehicle_types = [{"slug": s, **t} for s, t in self.custom_types.items()
                         if any(h in f"{s} {t.get('name','')}".lower() for h in VEHICLE_TYPE_HINTS)]
        out = {"site": {"name": "Arizona Kei Trucks", "url": self.base_url},
               "namespaces": list(self.namespaces),
               "woocommerce": {"present": WC_NAMESPACE in self.namespaces, "version": "9.1.2", "currency": "USD",
                               "api_enabled": self.woo_auth_ok},
               "auth": {"wordpress": {"user": "azkt-integration",
                                      "capabilities": sorted([k for k, v in self.wp_capabilities.items() if v]),
                                      "can_publish": bool(self.wp_capabilities.get("publish_posts"))},
                        "woocommerce": {"read": self.woo_auth_ok, "write": self.woo_auth_ok}},
               "post_types": {"post": {"slug": "post", "rest_base": "posts"},
                              **({"product": {"slug": "product", "rest_base": "products"}}
                                 if WC_NAMESPACE in self.namespaces else {}),
                              **{s: {"slug": s, **t} for s, t in self.custom_types.items()}},
               "vehicle_post_types": vehicle_types,
               "media": {"read": True, "upload": self.media_upload},
               "product_attributes": [{"id": 1, "name": "Transmission", "slug": "transmission"}],
               "product_categories": [{"id": 9, "name": "Kei Trucks", "slug": "kei-trucks"}],
               "limitations": [], "supported_ops": [o for o in OPERATIONS if o not in self.unsupported_ops]}
        if not self.woo_auth_ok:
            out["limitations"].append("WooCommerce system status is not readable (permission_denied); "
                                      "product writes are not proven")
        if vehicle_types:
            out["content_type"] = "custom"
            out["custom_post_type"] = vehicle_types[0].get("rest_base") or vehicle_types[0]["slug"]
        elif out["woocommerce"]["present"]:
            out["content_type"] = "product"
        else:
            out["content_type"] = "post"
        return out

    async def read_existing(self, *, sku: str | None = None, title: str | None = None,
                            external_id: str | None = None, profile: dict | None = None) -> list[dict]:
        self._guard("read_existing")
        rows = []
        for pid, row in self.items.items():
            if external_id and pid != external_id:
                continue
            if sku and (row.get("sku") or "") != sku:
                continue
            if title and not external_id and not sku:
                if title.lower() not in (row.get("name") or "").lower():
                    continue
            rows.append(_normalize_product(row))
        return rows

    def validate_package(self, package: dict, profile: dict | None) -> dict:
        return validate_package(package, profile)

    async def preview(self, package: dict, profile: dict | None) -> dict:
        self._guard("preview")
        res = validate_package(package, profile)
        return {"ok": res["ok"], "payload": res["payload"], "errors": res["errors"], "warnings": res["warnings"],
                "target": (profile or {}).get("staging_url") or self.base_url, "written": False}

    def _write(self, payload: dict, external_id: str | None) -> dict:
        pid = external_id or str(self.next_id)
        if external_id is None:
            self.next_id += 1
        row = dict(self.items.get(pid) or {})
        row.setdefault("id", pid)
        row.setdefault("images", [])
        row.setdefault("meta_data", [])
        row.setdefault("permalink", f"{self.base_url}/product/listing-{pid}")
        for k, v in payload.items():
            if k == "meta_data" and isinstance(v, dict):
                # WooCommerce upserts meta by key: an editor's own meta is never wiped by an AZKT write
                merged = {m.get("key"): m.get("value") for m in (row.get("meta_data") or []) if isinstance(m, dict)}
                merged.update(v)
                row["meta_data"] = [{"key": mk, "value": mv} for mk, mv in merged.items()]
            elif k == "images":
                row["images"] = [{"id": f"m{pid}-{i}", "src": m.get("src"), "alt": m.get("alt"),
                                  "sha256": m.get("sha256")} for i, m in enumerate(v or [])]
            else:
                row[k] = v
        row["_edited_by"] = "azkt"
        row["date_modified_gmt"] = "2026-09-15T13:00:00"
        self.items[pid] = row
        if not self.public_cache_lag:
            self.public[pid] = dict(row)
        return row

    async def upsert_draft(self, package: dict, profile: dict | None, external_id: str | None = None) -> dict:
        self._guard("upsert_draft")
        payload = {**render_payload(package, profile), "status": "draft"}
        row = self._write(payload, external_id)
        if self.fail_after_accept:
            self.fail_after_accept = False
            raise UnknownWriteResult("network failed after the site accepted the draft", provider_ref=row["id"])
        norm = _normalize_product(row)
        return {"state": "accepted", "external_id": norm["external_id"], "external_url": norm.get("url"),
                "status": norm.get("status"), "payload": payload}

    async def publish(self, external_id: str, package: dict, profile: dict | None) -> dict:
        self._guard("publish")
        payload = {**render_payload(package, profile), "status": "publish"}
        row = self._write(payload, external_id)
        if self.fail_after_accept:
            self.fail_after_accept = False
            raise UnknownWriteResult("network failed after the site accepted the publish", provider_ref=row["id"])
        norm = _normalize_product(row)
        return {"state": "published", "external_id": norm["external_id"], "external_url": norm.get("url"),
                "status": norm.get("status")}

    async def update_availability(self, external_id: str, availability: str, profile: dict | None) -> dict:
        self._guard("update_availability")
        mapping = availability_map_for(profile).get(availability)
        if mapping is None:
            raise Unsupported(f"the active site profile has no availability mapping for {availability!r}")
        payload = {k: v for k, v in mapping.items() if k != "purchasable"}
        row = self._write(payload, external_id)
        if self.fail_after_accept:
            self.fail_after_accept = False
            raise UnknownWriteResult("network failed after the site accepted the availability change",
                                     provider_ref=row["id"])
        return {"state": "accepted", "external_id": external_id, "availability": availability,
                "observed": _normalize_product(row), "payload": payload}

    async def read_back(self, external_id: str, *, profile: dict | None = None, cache_bust: bool = True) -> dict:
        self._guard("read_back")
        row = self.items.get(external_id)
        if row is None:
            raise _fail("invalid_input", f"no listing {external_id} on the site")
        api = _normalize_product(row)
        if self.fail_public_fetch:
            return {"api": api, "public": {"fetched": False, "error": "public page fetch failed"}}
        page = self.public.get(external_id) or row
        pub = _normalize_product(page)
        return {"api": api, "public": {"fetched": True, "status_code": 200, "cache": "HIT" if self.public_cache_lag else "MISS",
                                       "rendered": pub,
                                       "html": f"<h1>{pub.get('title')}</h1><span class='price'>{pub.get('price')}</span>"
                                               f"<span class='stock'>{pub.get('stock_status')}</span>"}}

    async def archive(self, external_id: str, profile: dict | None = None) -> dict:
        self._guard("archive")
        row = self._write({"status": "draft", "catalog_visibility": "hidden"}, external_id)
        return {"state": "unpublished", "external_id": external_id, "status": row.get("status")}

    async def install_plugin(self, *_a, **_kw):
        raise Unsupported("AZKT never installs plugins")

    async def update_site_settings(self, *_a, **_kw):
        self.settings_changes += 1
        raise Unsupported("payment/tax/shipping settings are read for context only and never changed")


# ── factory ──────────────────────────────────────────────────────────────────
_ADAPTER: Any | None = None


def set_adapter(adapter: Any | None) -> None:
    global _ADAPTER
    _ADAPTER = adapter


def current_adapter() -> Any | None:
    return _ADAPTER


def adapter_for(wp_conn=None, woo_conn=None, *, staging: bool = False) -> Any:
    """Build the website facade from the connection secrets, falling back to the environment."""
    if _ADAPTER is not None:
        return _ADAPTER
    from ..core.config import settings
    from ..services import connections as conn_svc
    wp_secret = conn_svc.get_secret(wp_conn) if wp_conn is not None else {}
    woo_secret = conn_svc.get_secret(woo_conn) if woo_conn is not None else {}
    site = ((wp_conn.config or {}).get("site_url") if wp_conn is not None else None) \
        or ((woo_conn.config or {}).get("site_url") if woo_conn is not None else None) or settings.WP_BASE_URL
    if staging:
        site = ((wp_conn.config or {}).get("staging_url") if wp_conn is not None else None) or settings.WP_STAGING_URL or site
    if not site:
        raise Unsupported("the website base URL is not configured; add it under Settings → Connections",
                          setup_blocked="wordpress.site_url")
    app_user = wp_secret.get("app_user") or settings.WP_APP_USER
    app_password = wp_secret.get("app_password") or settings.WP_APP_PASSWORD
    key = woo_secret.get("consumer_key") or settings.WC_CONSUMER_KEY
    secret = woo_secret.get("consumer_secret") or settings.WC_CONSUMER_SECRET
    if not (app_user and app_password) and not (key and secret):
        raise Unsupported("no website credentials are configured (WordPress application password or "
                          "WooCommerce key/secret)", setup_blocked="wordpress.credentials")
    if settings.ENV == "test":
        # a live client is never constructed in tests: install a fixture with `set_adapter`
        raise Unsupported("live website calls are disabled in tests; install a fixture adapter")
    wp = WordPressAdapter(site, app_user, app_password) if app_user and app_password else None
    woo = WooAdapter(site, key, secret) if key and secret else None
    return WebsiteAdapter(wp, woo, base_url=site)
