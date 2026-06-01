#!/usr/bin/env python3
"""
Meta Ads MCP Server — Happie Beverages

FastMCP server wrapping Meta Marketing API v21.
Implements full OAuth 2.0 so Cowork can connect via Add Custom Connector.
Tokens last 1 year; re-auth only needed after a Railway redeploy.

Required env var:
  META_ACCESS_TOKEN       — non-expiring System User token (ads_read + read_insights)
  RAILWAY_PUBLIC_DOMAIN   — auto-set by Railway (e.g. web-production-99121.up.railway.app)

Optional:
  META_API_VERSION        — default v21.0
  PORT                    — default 8000
"""

import json
import logging
import os
import secrets
import time
from typing import Optional, Union

import httpx
from pydantic import BaseModel, Field, field_validator, ConfigDict
from mcp.server.fastmcp import FastMCP
from mcp.server.auth.provider import (
    OAuthAuthorizationServerProvider,
    AuthorizationParams,
    AuthorizationCode,
    AccessToken,
    RefreshToken,
)
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_VERSION = os.environ.get("META_API_VERSION", "v21.0")
GRAPH_BASE  = f"https://graph.facebook.com/{API_VERSION}"
META_TOKEN  = os.environ.get("META_ACCESS_TOKEN", "")
PORT        = int(os.environ.get("PORT", 8000))

_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", f"localhost:{PORT}")
SERVER_URL = _domain if _domain.startswith("http") else f"https://{_domain}"

HAPPIE_ACCOUNTS = {
    "act_4265171330413775": "Happie Ads (Alec — brand/warmup)",
    "act_1473338457823788": "Happie Fusion (Muhammad — FF acquisition)",
}

INSIGHT_FIELDS = ",".join([
    "spend", "impressions", "reach", "clicks", "cpm", "ctr",
    "inline_link_clicks", "inline_link_click_ctr", "cpc",
    "actions", "action_values", "cost_per_action_type", "website_purchase_roas",
])
AD_INSIGHT_FIELDS = ",".join([
    "ad_id", "ad_name", "adset_name", "campaign_name",
    "spend", "impressions", "inline_link_click_ctr", "inline_link_clicks",
    "cpm", "actions", "action_values", "website_purchase_roas",
])

# ---------------------------------------------------------------------------
# OAuth 2.0 Provider — in-memory, auto-approves all clients
# ---------------------------------------------------------------------------

class InMemoryOAuthProvider(OAuthAuthorizationServerProvider):
    """
    Minimal OAuth server. Supports dynamic client registration and
    auto-approves the authorization step (trusted internal server).
    Access tokens last 1 year.
    """

    def __init__(self) -> None:
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._codes:   dict[str, AuthorizationCode]          = {}
        self._tokens:  dict[str, AccessToken]                = {}

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info
        logger.info("OAuth client registered: %s", client_info.client_id)

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """Auto-approve: generate auth code immediately (no human gate)."""
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or ["mcp"],
            expires_at=time.time() + 300,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        logger.info("Auto-approved auth for client: %s", client.client_id)
        return code

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> Optional[AuthorizationCode]:
        obj = self._codes.get(authorization_code)
        if not obj or obj.expires_at < time.time() or obj.client_id != client.client_id:
            return None
        return obj

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        self._codes.pop(authorization_code.code, None)
        token = secrets.token_urlsafe(32)
        year  = 365 * 24 * 3600
        self._tokens[token] = AccessToken(
            token=token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(time.time()) + year,
        )
        logger.info("Access token issued for client: %s", client.client_id)
        return OAuthToken(
            access_token=token,
            token_type="Bearer",
            expires_in=year,
            scope=" ".join(authorization_code.scopes),
        )

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        obj = self._tokens.get(token)
        if not obj:
            return None
        if obj.expires_at and obj.expires_at < time.time():
            del self._tokens[token]
            return None
        return obj

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> Optional[RefreshToken]:
        return None  # 1-year access tokens; no refresh needed

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list,
    ) -> OAuthToken:
        raise NotImplementedError

    async def revoke_token(
        self,
        token: Union[AccessToken, RefreshToken],
    ) -> None:
        if isinstance(token, AccessToken):
            self._tokens.pop(token.token, None)


# ---------------------------------------------------------------------------
# Registration normalizer — Cowork omits refresh_token from grant_types
# but FastMCP 1.26+ requires it. This middleware patches it transparently.
# ---------------------------------------------------------------------------

class RegistrationNormalizerMiddleware:
    def __init__(self, app) -> None:
        self._app = app

    async def __call__(self, scope, receive, send) -> None:
        if (scope.get("type") == "http"
                and scope.get("path") == "/register"
                and scope.get("method") == "POST"):
            chunks, more = [], True
            while more:
                msg = await receive()
                chunks.append(msg.get("body", b""))
                more = msg.get("more_body", False)
            body = b"".join(chunks)
            try:
                data = __import__("json").loads(body)
                gt = data.get("grant_types", ["authorization_code"])
                if "refresh_token" not in gt:
                    data["grant_types"] = list(gt) + ["refresh_token"]
                body = __import__("json").dumps(data).encode()
                logger.info("RegistrationNormalizer: patched grant_types → %s", data["grant_types"])
            except Exception as e:
                logger.warning("RegistrationNormalizer: body parse failed: %s", e)
            hdrs = [(k,v) for k,v in scope.get("headers",[]) if k.lower() != b"content-length"]
            hdrs.append((b"content-length", str(len(body)).encode()))
            scope = {**scope, "headers": hdrs}
            sent = False
            async def patched_receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return {"type": "http.disconnect"}
            await self._app(scope, patched_receive, send)
        else:
            await self._app(scope, receive, send)

# ---------------------------------------------------------------------------
# FastMCP server with OAuth
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "meta_ads_mcp",
    host="0.0.0.0",
    port=PORT,
    auth_server_provider=InMemoryOAuthProvider(),
    auth=AuthSettings(
        issuer_url=SERVER_URL,
        resource_server_url=SERVER_URL,
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["mcp"],
            default_scopes=["mcp"],
        ),
        revocation_options=RevocationOptions(enabled=True),
    ),
)

# ---------------------------------------------------------------------------
# Shared Graph API utilities
# ---------------------------------------------------------------------------

async def _graph(path: str, params: dict) -> dict:
    params["access_token"] = META_TOKEN
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{GRAPH_BASE}/{path}", params=params)
        r.raise_for_status()
        return r.json()

def _extract_action(items: list, action_type: str, default: float = 0.0) -> float:
    for item in (items or []):
        if item.get("action_type") == action_type:
            return float(item.get("value", default))
    return default

def _extract_roas(roas_list: list) -> float:
    return round(sum(float(r.get("value", 0)) for r in (roas_list or [])), 2)

def _fmt(row: dict) -> dict:
    actions = row.get("actions") or []
    avals   = row.get("action_values") or []
    cpa_lst = row.get("cost_per_action_type") or []
    roas    = row.get("website_purchase_roas") or []
    purchases = (_extract_action(actions, "offsite_conversion.fb_pixel_purchase")
                 or _extract_action(actions, "purchase"))
    revenue   = (_extract_action(avals, "offsite_conversion.fb_pixel_purchase")
                 or _extract_action(avals, "purchase"))
    cpa       = (_extract_action(cpa_lst, "offsite_conversion.fb_pixel_purchase")
                 or _extract_action(cpa_lst, "purchase"))
    return {
        "spend":       round(float(row.get("spend", 0)), 2),
        "impressions": int(row.get("impressions", 0)),
        "reach":       int(row.get("reach", 0)),
        "link_clicks": int(row.get("inline_link_clicks", 0)),
        "cpm":         round(float(row.get("cpm", 0)), 2),
        "link_ctr":    round(float(row.get("inline_link_click_ctr", 0)), 4),
        "cpc":         round(float(row.get("cpc", 0)), 2),
        "purchases":   int(purchases),
        "revenue":     round(revenue, 2),
        "cpa":         round(cpa, 2),
        "roas":        _extract_roas(roas),
    }

def _err(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        try:
            body = e.response.json(); err = body.get("error", {})
            code = err.get("code", e.response.status_code)
            msg  = err.get("message", str(e))
            if code in (190, 102):
                return f"Error: Meta token expired/invalid (code {code}). Refresh META_ACCESS_TOKEN."
            return f"Error {code}: {msg}"
        except Exception:
            return f"Error: HTTP {e.response.status_code} from Meta Graph API."
    if isinstance(e, httpx.TimeoutException):
        return "Error: Request timed out — retry."
    return f"Error: {type(e).__name__}: {e}"

def _date_params(preset: "DatePreset", start: Optional[str], end: Optional[str]) -> dict:
    if start and end:
        return {"time_range": json.dumps({"since": start, "until": end})}
    return {"date_preset": preset.value}

# ---------------------------------------------------------------------------
# Enums + Input models
# ---------------------------------------------------------------------------
from enum import Enum

class DatePreset(str, Enum):
    LAST_7D="last_7d"; LAST_14D="last_14d"; LAST_30D="last_30d"
    THIS_MONTH="this_month"; LAST_MONTH="last_month"; YESTERDAY="yesterday"

class SortBy(str, Enum):
    LINK_CTR="link_ctr"; SPEND="spend"; ROAS="roas"; PURCHASES="purchases"; IMPRESSIONS="impressions"

def _norm(v: str) -> str:
    return v if v.startswith("act_") else f"act_{v}"

class AccountInsightsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    account_id: str = Field(..., description="Meta ad account ID. Happie: act_4265171330413775 (Happie Ads) or act_1473338457823788 (Happie Fusion).")
    date_preset: DatePreset = Field(DatePreset.LAST_30D)
    start_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date:   Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    @field_validator("account_id")
    @classmethod
    def normalize(cls, v: str) -> str:
        return _norm(v)

class CampaignInsightsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    account_id: str = Field(...)
    date_preset: DatePreset = Field(DatePreset.LAST_30D)
    start_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date:   Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    status_filter: Optional[str] = Field(None)
    @field_validator("account_id")
    @classmethod
    def normalize(cls, v: str) -> str:
        return _norm(v)

class TopAdsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    account_id: str = Field(...)
    date_preset: DatePreset = Field(DatePreset.LAST_30D)
    sort_by: SortBy = Field(SortBy.LINK_CTR); limit: int = Field(5, ge=1, le=20)
    start_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date:   Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    @field_validator("account_id")
    @classmethod
    def normalize(cls, v: str) -> str:
        return _norm(v)

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(name="meta_ads_auth_check", annotations={"readOnlyHint": True, "destructiveHint": False})
async def meta_ads_auth_check() -> str:
    """Verify META_ACCESS_TOKEN is valid and list accessible ad accounts."""
    try:
        me = await _graph("me", {"fields": "id,name"})
        accts = await _graph("me/adaccounts", {"fields": "id,name,account_status,currency"})
        return json.dumps({"token_status": "valid", "user": me, "ad_accounts": accts.get("data", []), "happie_accounts": HAPPIE_ACCOUNTS}, indent=2)
    except Exception as e:
        return _err(e)

@mcp.tool(name="meta_ads_get_account_insights", annotations={"readOnlyHint": True, "destructiveHint": False})
async def meta_ads_get_account_insights(params: AccountInsightsInput) -> str:
    """Get aggregate performance for a Meta ad account: spend, ROAS, CPA, CPM, CTR, purchases, revenue."""
    try:
        dp = _date_params(params.date_preset, params.start_date, params.end_date)
        raw = await _graph(f"{params.account_id}/insights", {"fields": INSIGHT_FIELDS, "level": "account", **dp})
        data = raw.get("data", [])
        if not data:
            return json.dumps({"account_id": params.account_id, "period": dp, "summary": "No data — no active ads in this period."}, indent=2)
        row = _fmt(data[0])
        verdict = "scale" if row["roas"] >= 2.0 else "hold" if row["roas"] >= 1.0 else "cut" if row["spend"] > 0 else "insufficient_data"
        return json.dumps({"account_id": params.account_id, "account_name": HAPPIE_ACCOUNTS.get(params.account_id, params.account_id), "period": dp, "summary": row, "verdict": verdict}, indent=2)
    except Exception as e:
        return _err(e)

@mcp.tool(name="meta_ads_get_campaign_insights", annotations={"readOnlyHint": True, "destructiveHint": False})
async def meta_ads_get_campaign_insights(params: CampaignInsightsInput) -> str:
    """Per-campaign breakdown: spend, ROAS, CPA, verdict (scale/hold/cut), sorted by spend desc."""
    try:
        dp = _date_params(params.date_preset, params.start_date, params.end_date)
        raw = await _graph(f"{params.account_id}/insights", {"fields": INSIGHT_FIELDS + ",campaign_id,campaign_name", "level": "campaign", **dp, "limit": 100})
        campaigns = []
        for row in raw.get("data", []):
            f = _fmt(row)
            if params.status_filter and params.status_filter != "ALL" and row.get("campaign_status") != params.status_filter:
                continue
            campaigns.append({"campaign_id": row.get("campaign_id"), "campaign_name": row.get("campaign_name"), "metrics": f, "verdict": "scale" if f["roas"] >= 2.0 else "hold" if f["roas"] >= 1.0 else "cut" if f["spend"] > 0 else "insufficient_data"})
        campaigns.sort(key=lambda c: c["metrics"]["spend"], reverse=True)
        return json.dumps({"account_id": params.account_id, "account_name": HAPPIE_ACCOUNTS.get(params.account_id, params.account_id), "period": dp, "campaigns": campaigns}, indent=2)
    except Exception as e:
        return _err(e)

@mcp.tool(name="meta_ads_get_top_ads", annotations={"readOnlyHint": True, "destructiveHint": False})
async def meta_ads_get_top_ads(params: TopAdsInput) -> str:
    """Top N ads ranked by link_ctr, spend, roas, purchases, or impressions. Use for creative reviews."""
    try:
        dp = _date_params(params.date_preset, params.start_date, params.end_date)
        raw = await _graph(f"{params.account_id}/insights", {"fields": AD_INSIGHT_FIELDS, "level": "ad", **dp, "limit": 200})
        ads = [{"ad_id": r.get("ad_id"), "ad_name": r.get("ad_name"), "adset_name": r.get("adset_name"), "campaign_name": r.get("campaign_name"), "metrics": _fmt(r)} for r in raw.get("data", []) if int(r.get("impressions", 0)) >= 10]
        ads.sort(key=lambda a: a["metrics"][params.sort_by.value], reverse=True)
        return json.dumps({"account_id": params.account_id, "account_name": HAPPIE_ACCOUNTS.get(params.account_id, params.account_id), "period": dp, "sort_by": params.sort_by.value, "top_ads": ads[:params.limit]}, indent=2)
    except Exception as e:
        return _err(e)

@mcp.tool(name="meta_ads_list_campaigns", annotations={"readOnlyHint": True, "destructiveHint": False})
async def meta_ads_list_campaigns(account_id: str, status: str = "ACTIVE") -> str:
    """List campaigns with budget, objective, status. status: ACTIVE | PAUSED | ARCHIVED | ALL."""
    try:
        if not account_id.startswith("act_"): account_id = f"act_{account_id}"
        effective = [status] if status != "ALL" else ["ACTIVE","PAUSED","ARCHIVED","DELETED"]
        raw = await _graph(f"{account_id}/campaigns", {"fields": "id,name,objective,status,effective_status,daily_budget,lifetime_budget", "effective_status": json.dumps(effective), "limit": 100})
        campaigns = [{"id": c.get("id"), "name": c.get("name"), "objective": c.get("objective"), "status": c.get("effective_status"), "daily_budget_usd": round(int(c["daily_budget"])/100,2) if c.get("daily_budget") else None} for c in raw.get("data", [])]
        return json.dumps({"account_id": account_id, "account_name": HAPPIE_ACCOUNTS.get(account_id, account_id), "count": len(campaigns), "campaigns": campaigns}, indent=2)
    except Exception as e:
        return _err(e)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    logger.info("Starting meta_ads_mcp at %s (port %d)", SERVER_URL, PORT)
    app = RegistrationNormalizerMiddleware(mcp.streamable_http_app())
    uvicorn.run(app, host="0.0.0.0", port=PORT)
