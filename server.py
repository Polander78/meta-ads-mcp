#!/usr/bin/env python3
"""
Meta Ads MCP Server

FastMCP server for the Meta Marketing API v21.
Pulls ad performance data for Happie Beverages without Supermetrics dependency.

Required env vars:
  META_ACCESS_TOKEN  — non-expiring System User token (ads_read + read_insights)
  MCP_BEARER_TOKEN   — pre-shared bearer token Cowork sends on every request

Optional env vars:
  META_API_VERSION   — default: v21.0
  PORT               — default: 8000
"""

import json
import logging
import os
import secrets
from typing import Optional
from enum import Enum

import httpx
import uvicorn
from pydantic import BaseModel, Field, field_validator, ConfigDict
from mcp.server.fastmcp import FastMCP
from starlette.types import ASGIApp, Receive, Scope, Send

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Server init
# ---------------------------------------------------------------------------
mcp = FastMCP("meta_ads_mcp")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
API_VERSION = os.environ.get("META_API_VERSION", "v21.0")
GRAPH_BASE  = f"https://graph.facebook.com/{API_VERSION}"
TOKEN       = os.environ.get("META_ACCESS_TOKEN", "")
BEARER      = os.environ.get("MCP_BEARER_TOKEN", "")

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
# Bearer-token ASGI middleware  (same pattern as multi-brand-mcp)
# ---------------------------------------------------------------------------
_PUBLIC = ["/.well-known/", "/health"]

async def _send_401(send: Send, reason: str) -> None:
    body = json.dumps({"error": "unauthorized", "reason": reason}).encode()
    await send({"type": "http.response.start", "status": 401,
                "headers": [[b"content-type", b"application/json"],
                             [b"content-length", str(len(body)).encode()],
                             [b"www-authenticate", b"Bearer"]]})
    await send({"type": "http.response.body", "body": body, "more_body": False})


class BearerTokenMiddleware:
    """Pure ASGI middleware — validates Authorization: Bearer <token> header."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app
        if not BEARER:
            logger.warning("MCP_BEARER_TOKEN not set — open access (dev mode only)")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not BEARER:
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")
        if any(path.startswith(p) for p in _PUBLIC):
            await self._app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        auth    = headers.get(b"authorization", b"").decode()
        if not auth.lower().startswith("bearer "):
            await _send_401(send, "missing_bearer")
            return

        provided = auth[7:].strip()
        if not secrets.compare_digest(provided, BEARER):
            await _send_401(send, "invalid_bearer")
            return

        await self._app(scope, receive, send)

# ---------------------------------------------------------------------------
# Shared Graph API utilities
# ---------------------------------------------------------------------------

async def _graph(path: str, params: dict) -> dict:
    params["access_token"] = TOKEN
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
    if not roas_list:
        return 0.0
    return round(sum(float(r.get("value", 0)) for r in roas_list), 2)


def _format_insights_row(row: dict) -> dict:
    actions     = row.get("actions") or []
    action_vals = row.get("action_values") or []
    cost_per    = row.get("cost_per_action_type") or []
    roas_list   = row.get("website_purchase_roas") or []

    purchases = (_extract_action(actions, "offsite_conversion.fb_pixel_purchase")
                 or _extract_action(actions, "purchase"))
    revenue   = (_extract_action(action_vals, "offsite_conversion.fb_pixel_purchase")
                 or _extract_action(action_vals, "purchase"))
    cpa       = (_extract_action(cost_per, "offsite_conversion.fb_pixel_purchase")
                 or _extract_action(cost_per, "purchase"))

    return {
        "spend":       round(float(row.get("spend", 0)), 2),
        "impressions": int(row.get("impressions", 0)),
        "reach":       int(row.get("reach", 0)),
        "clicks":      int(row.get("clicks", 0)),
        "link_clicks": int(row.get("inline_link_clicks", 0)),
        "cpm":         round(float(row.get("cpm", 0)), 2),
        "ctr_all":     round(float(row.get("ctr", 0)), 4),
        "link_ctr":    round(float(row.get("inline_link_click_ctr", 0)), 4),
        "cpc":         round(float(row.get("cpc", 0)), 2),
        "purchases":   int(purchases),
        "revenue":     round(revenue, 2),
        "cpa":         round(cpa, 2),
        "roas":        _extract_roas(roas_list),
    }


def _handle_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        try:
            body = e.response.json()
            err  = body.get("error", {})
            code = err.get("code", e.response.status_code)
            msg  = err.get("message", str(e))
            if code in (190, 102):
                return f"Error: Meta token expired/invalid (code {code}). Refresh META_ACCESS_TOKEN."
            if code == 200:
                return f"Error: Insufficient permissions — ensure ads_read + read_insights. ({msg})"
            return f"Error {code}: {msg}"
        except Exception:
            return f"Error: HTTP {e.response.status_code} from Meta Graph API."
    if isinstance(e, httpx.TimeoutException):
        return "Error: Request timed out — retry in a moment."
    return f"Error: {type(e).__name__}: {e}"


def _date_params(date_preset: "DatePreset", start_date: Optional[str], end_date: Optional[str]) -> dict:
    if start_date and end_date:
        return {"time_range": json.dumps({"since": start_date, "until": end_date})}
    return {"date_preset": date_preset.value}

# ---------------------------------------------------------------------------
# Enums + Input models
# ---------------------------------------------------------------------------

class DatePreset(str, Enum):
    LAST_7D    = "last_7d"
    LAST_14D   = "last_14d"
    LAST_30D   = "last_30d"
    THIS_MONTH = "this_month"
    LAST_MONTH = "last_month"
    LAST_90D   = "last_90d"
    YESTERDAY  = "yesterday"
    TODAY      = "today"


class SortBy(str, Enum):
    LINK_CTR    = "link_ctr"
    SPEND       = "spend"
    ROAS        = "roas"
    PURCHASES   = "purchases"
    IMPRESSIONS = "impressions"


class AccountInsightsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)
    account_id:  str        = Field(..., description="Meta ad account ID, e.g. 'act_4265171330413775'. Happie defaults: act_4265171330413775 (Happie Ads) or act_1473338457823788 (Happie Fusion).")
    date_preset: DatePreset = Field(DatePreset.LAST_30D, description="last_7d | last_14d | last_30d | this_month | last_month | last_90d | yesterday | today")
    start_date:  Optional[str] = Field(None, description="Custom start YYYY-MM-DD (overrides date_preset).", pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date:    Optional[str] = Field(None, description="Custom end YYYY-MM-DD (inclusive).",               pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("account_id")
    @classmethod
    def normalize(cls, v: str) -> str:
        return v if v.startswith("act_") else f"act_{v}"


class CampaignInsightsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)
    account_id:    str        = Field(..., description="Meta ad account ID.")
    date_preset:   DatePreset = Field(DatePreset.LAST_30D)
    start_date:    Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date:      Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    status_filter: Optional[str] = Field(None, description="ACTIVE | PAUSED | ARCHIVED | ALL")

    @field_validator("account_id")
    @classmethod
    def normalize(cls, v: str) -> str:
        return v if v.startswith("act_") else f"act_{v}"


class TopAdsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)
    account_id:  str        = Field(..., description="Meta ad account ID.")
    date_preset: DatePreset = Field(DatePreset.LAST_30D)
    sort_by:     SortBy     = Field(SortBy.LINK_CTR, description="link_ctr | spend | roas | purchases | impressions")
    limit:       int        = Field(5, ge=1, le=20, description="Top N ads to return (1–20).")
    start_date:  Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date:    Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("account_id")
    @classmethod
    def normalize(cls, v: str) -> str:
        return v if v.startswith("act_") else f"act_{v}"

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(name="meta_ads_auth_check", annotations={"title": "Meta Ads Auth Check", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def meta_ads_auth_check() -> str:
    """Verify META_ACCESS_TOKEN is valid and list accessible ad accounts."""
    try:
        me       = await _graph("me", {"fields": "id,name"})
        accounts = await _graph("me/adaccounts", {"fields": "id,name,account_status,currency,timezone_name"})
        return json.dumps({"token_status": "valid", "user": {"id": me.get("id"), "name": me.get("name")}, "ad_accounts": accounts.get("data", []), "happie_accounts": HAPPIE_ACCOUNTS}, indent=2)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="meta_ads_get_account_insights", annotations={"title": "Meta Ads — Account Insights", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def meta_ads_get_account_insights(params: AccountInsightsInput) -> str:
    """Get aggregate ad performance for a Meta ad account (spend, ROAS, CPA, CPM, CTR, purchases, revenue)."""
    try:
        date_p = _date_params(params.date_preset, params.start_date, params.end_date)
        raw    = await _graph(f"{params.account_id}/insights", {"fields": INSIGHT_FIELDS, "level": "account", **date_p})
        data   = raw.get("data", [])
        if not data:
            return json.dumps({"account_id": params.account_id, "period": date_p, "summary": "No data — no active ads in this period."}, indent=2)
        row = _format_insights_row(data[0])
        return json.dumps({"account_id": params.account_id, "account_name": HAPPIE_ACCOUNTS.get(params.account_id, params.account_id), "period": date_p, "summary": row, "verdict": "scale" if row["roas"] >= 2.0 else "hold" if row["roas"] >= 1.0 else "cut" if row["spend"] > 0 else "insufficient_data"}, indent=2)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="meta_ads_get_campaign_insights", annotations={"title": "Meta Ads — Campaign Breakdown", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def meta_ads_get_campaign_insights(params: CampaignInsightsInput) -> str:
    """Per-campaign performance breakdown — spend, ROAS, CPA, verdict (scale/hold/cut), sorted by spend."""
    try:
        date_p = _date_params(params.date_preset, params.start_date, params.end_date)
        raw    = await _graph(f"{params.account_id}/insights", {"fields": INSIGHT_FIELDS + ",campaign_id,campaign_name", "level": "campaign", **date_p, "limit": 100})
        campaigns = []
        for row in raw.get("data", []):
            fmt = _format_insights_row(row)
            if params.status_filter and params.status_filter != "ALL" and row.get("campaign_status") != params.status_filter:
                continue
            campaigns.append({"campaign_id": row.get("campaign_id"), "campaign_name": row.get("campaign_name"), "metrics": fmt, "verdict": "scale" if fmt["roas"] >= 2.0 else "hold" if fmt["roas"] >= 1.0 else "cut" if fmt["spend"] > 0 else "insufficient_data"})
        campaigns.sort(key=lambda c: c["metrics"]["spend"], reverse=True)
        return json.dumps({"account_id": params.account_id, "account_name": HAPPIE_ACCOUNTS.get(params.account_id, params.account_id), "period": date_p, "campaign_count": len(campaigns), "campaigns": campaigns}, indent=2)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="meta_ads_get_top_ads", annotations={"title": "Meta Ads — Top Ads", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def meta_ads_get_top_ads(params: TopAdsInput) -> str:
    """Top N ads ranked by link_ctr, spend, roas, purchases, or impressions. Use for creative reviews."""
    try:
        date_p   = _date_params(params.date_preset, params.start_date, params.end_date)
        raw      = await _graph(f"{params.account_id}/insights", {"fields": AD_INSIGHT_FIELDS, "level": "ad", **date_p, "limit": 200})
        sort_key = params.sort_by.value
        ads = [{"ad_id": r.get("ad_id"), "ad_name": r.get("ad_name"), "adset_name": r.get("adset_name"), "campaign_name": r.get("campaign_name"), "metrics": _format_insights_row(r)} for r in raw.get("data", []) if int(r.get("impressions", 0)) >= 10]
        ads.sort(key=lambda a: a["metrics"][sort_key], reverse=True)
        return json.dumps({"account_id": params.account_id, "account_name": HAPPIE_ACCOUNTS.get(params.account_id, params.account_id), "period": date_p, "sort_by": sort_key, "top_ads": ads[:params.limit]}, indent=2)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="meta_ads_list_campaigns", annotations={"title": "Meta Ads — List Campaigns", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def meta_ads_list_campaigns(account_id: str, status: str = "ACTIVE") -> str:
    """List campaigns with budget, objective, and status. status: ACTIVE | PAUSED | ARCHIVED | ALL."""
    try:
        if not account_id.startswith("act_"):
            account_id = f"act_{account_id}"
        effective = [status] if status != "ALL" else ["ACTIVE", "PAUSED", "ARCHIVED", "DELETED"]
        raw = await _graph(f"{account_id}/campaigns", {"fields": "id,name,objective,status,effective_status,daily_budget,lifetime_budget,start_time,stop_time", "effective_status": json.dumps(effective), "limit": 100})
        campaigns = [{"id": c.get("id"), "name": c.get("name"), "objective": c.get("objective"), "status": c.get("effective_status"), "daily_budget_usd": round(int(c["daily_budget"]) / 100, 2) if c.get("daily_budget") else None, "lifetime_budget_usd": round(int(c["lifetime_budget"]) / 100, 2) if c.get("lifetime_budget") else None} for c in raw.get("data", [])]
        return json.dumps({"account_id": account_id, "account_name": HAPPIE_ACCOUNTS.get(account_id, account_id), "status_filter": status, "count": len(campaigns), "campaigns": campaigns}, indent=2)
    except Exception as e:
        return _handle_error(e)


# ---------------------------------------------------------------------------
# Entry point — wrap FastMCP app with BearerTokenMiddleware
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app  = BearerTokenMiddleware(mcp.streamable_http_app())
    uvicorn.run(app, host="0.0.0.0", port=port)
