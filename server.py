#!/usr/bin/env python3
"""
Meta Ads MCP Server

FastMCP server for the Meta Marketing API v21.
Pulls ad performance data for Happie Beverages without Supermetrics dependency.

Required env var:
  META_ACCESS_TOKEN — long-lived System User token with ads_read + read_insights

Optional env vars:
  META_API_VERSION   — default: v21.0
  PORT               — default: 8000
"""

import json
import os
from typing import Optional
from enum import Enum

import httpx
from pydantic import BaseModel, Field, field_validator, ConfigDict
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Server init
# ---------------------------------------------------------------------------
mcp = FastMCP("meta_ads_mcp")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
API_VERSION = os.environ.get("META_API_VERSION", "v21.0")
GRAPH_BASE = f"https://graph.facebook.com/{API_VERSION}"
TOKEN = os.environ.get("META_ACCESS_TOKEN", "")

# Happie Beverages ad accounts (fallback defaults)
HAPPIE_ACCOUNTS = {
    "act_4265171330413775": "Happie Ads (Alec — brand/warmup)",
    "act_1473338457823788": "Happie Fusion (Muhammad — FF acquisition)",
}

# Standard insight fields for every account/campaign call
INSIGHT_FIELDS = ",".join([
    "spend", "impressions", "reach", "clicks", "cpm", "ctr",
    "inline_link_clicks", "inline_link_click_ctr", "cpc",
    "actions", "action_values", "cost_per_action_type",
    "website_purchase_roas",
])

# Lighter field set for ad-level queries (faster report generation)
AD_INSIGHT_FIELDS = ",".join([
    "ad_id", "ad_name", "adset_name", "campaign_name",
    "spend", "impressions", "inline_link_click_ctr", "inline_link_clicks",
    "cpm", "actions", "action_values", "website_purchase_roas",
])

# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

async def _graph(path: str, params: dict) -> dict:
    """Authenticated GET to the Graph API."""
    params["access_token"] = TOKEN
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{GRAPH_BASE}/{path}", params=params)
        r.raise_for_status()
        return r.json()

def _extract_action(items: list, action_type: str, default: float = 0.0) -> float:
    """Pull a single numeric value from a Meta actions/action_values array."""
    for item in (items or []):
        if item.get("action_type") == action_type:
            return float(item.get("value", default))
    return default


def _extract_roas(roas_list: list) -> float:
    """Return overall website purchase ROAS from the roas array."""
    if not roas_list:
        return 0.0
    return round(sum(float(r.get("value", 0)) for r in roas_list), 2)


def _format_insights_row(row: dict) -> dict:
    """Normalise a raw Graph API insights row into a clean dict."""
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
    roas      = _extract_roas(roas_list)
    spend     = float(row.get("spend", 0))

    return {
        "spend":       round(spend, 2),
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
        "roas":        roas,
    }

def _handle_error(e: Exception) -> str:
    """Convert exceptions into actionable error strings."""
    if isinstance(e, httpx.HTTPStatusError):
        try:
            body = e.response.json()
            err  = body.get("error", {})
            msg  = err.get("message", str(e))
            code = err.get("code", e.response.status_code)
            if code in (190, 102):
                return f"Error: Meta access token expired or invalid (code {code}). Refresh META_ACCESS_TOKEN env var."
            if code == 200:
                return f"Error: Insufficient permissions — ensure ads_read + read_insights are granted. ({msg})"
            if code == 10:
                return f"Error: Permission denied — check ad account ID and token scope. ({msg})"
            return f"Error {code}: {msg}"
        except Exception:
            return f"Error: HTTP {e.response.status_code} from Meta Graph API."
    if isinstance(e, httpx.TimeoutException):
        return "Error: Request timed out. Meta API may be slow — retry in a moment."
    return f"Error: {type(e).__name__}: {e}"


def _date_params(date_preset: "DatePreset", start_date: Optional[str], end_date: Optional[str]) -> dict:
    """Return either time_range or date_preset for an insights call."""
    if start_date and end_date:
        return {"time_range": json.dumps({"since": start_date, "until": end_date})}
    return {"date_preset": date_preset.value}


# ---------------------------------------------------------------------------
# Enums
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

# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------

class AccountInsightsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)
    account_id:  str        = Field(..., description="Meta ad account ID, e.g. 'act_4265171330413775'. Happie defaults: act_4265171330413775 (Happie Ads) or act_1473338457823788 (Happie Fusion).")
    date_preset: DatePreset = Field(DatePreset.LAST_30D, description="Relative date window: last_7d, last_14d, last_30d, this_month, last_month, last_90d, yesterday, today.")
    start_date:  Optional[str] = Field(None, description="Custom start YYYY-MM-DD (overrides date_preset).", pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date:    Optional[str] = Field(None, description="Custom end YYYY-MM-DD (inclusive).",               pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("account_id")
    @classmethod
    def normalize(cls, v: str) -> str:
        return v if v.startswith("act_") else f"act_{v}"


class CampaignInsightsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)
    account_id:    str        = Field(..., description="Meta ad account ID.")
    date_preset:   DatePreset = Field(DatePreset.LAST_30D, description="Relative date window.")
    start_date:    Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date:      Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    status_filter: Optional[str] = Field(None, description="ACTIVE, PAUSED, ARCHIVED, or ALL.")

    @field_validator("account_id")
    @classmethod
    def normalize(cls, v: str) -> str:
        return v if v.startswith("act_") else f"act_{v}"


class TopAdsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)
    account_id:  str        = Field(..., description="Meta ad account ID.")
    date_preset: DatePreset = Field(DatePreset.LAST_30D, description="Relative date window.")
    sort_by:     SortBy     = Field(SortBy.LINK_CTR,    description="Rank by: link_ctr, spend, roas, purchases, impressions.")
    limit:       int        = Field(5, ge=1, le=20,     description="Number of top ads to return (1–20).")
    start_date:  Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date:    Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("account_id")
    @classmethod
    def normalize(cls, v: str) -> str:
        return v if v.startswith("act_") else f"act_{v}"

# ---------------------------------------------------------------------------
# Tool: auth check
# ---------------------------------------------------------------------------

@mcp.tool(name="meta_ads_auth_check", annotations={"title": "Meta Ads Auth Check", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def meta_ads_auth_check() -> str:
    """Verify META_ACCESS_TOKEN is valid and list accessible ad accounts.
    Call this first to confirm the token is working. Returns token metadata
    and the list of ad accounts the token can reach.
    """
    try:
        me       = await _graph("me", {"fields": "id,name"})
        accounts = await _graph("me/adaccounts", {"fields": "id,name,account_status,currency,timezone_name"})
        return json.dumps({
            "token_status": "valid",
            "user":          {"id": me.get("id"), "name": me.get("name")},
            "ad_accounts":   accounts.get("data", []),
            "happie_accounts": HAPPIE_ACCOUNTS,
        }, indent=2)
    except Exception as e:
        return _handle_error(e)


# ---------------------------------------------------------------------------
# Tool: account-level insights
# ---------------------------------------------------------------------------

@mcp.tool(name="meta_ads_get_account_insights", annotations={"title": "Meta Ads — Account Insights", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def meta_ads_get_account_insights(params: AccountInsightsInput) -> str:
    """Get aggregate ad performance for one Meta ad account over a date window.
    Returns spend, impressions, reach, CPM, CTR, link clicks, website purchases,
    attributed revenue, CPA, and ROAS. Use for Monday ROI briefing account summary.
    """
    try:
        date_p = _date_params(params.date_preset, params.start_date, params.end_date)
        raw    = await _graph(f"{params.account_id}/insights", {"fields": INSIGHT_FIELDS, "level": "account", **date_p})
        data   = raw.get("data", [])
        if not data:
            return json.dumps({"account_id": params.account_id, "account_name": HAPPIE_ACCOUNTS.get(params.account_id, params.account_id), "period": date_p, "summary": "No data — account may have had no active ads in this period."}, indent=2)
        row = _format_insights_row(data[0])
        return json.dumps({
            "account_id":   params.account_id,
            "account_name": HAPPIE_ACCOUNTS.get(params.account_id, params.account_id),
            "period":       date_p,
            "summary":      row,
            "verdict":      "scale" if row["roas"] >= 2.0 else "hold" if row["roas"] >= 1.0 else "cut" if row["spend"] > 0 else "insufficient_data",
        }, indent=2)
    except Exception as e:
        return _handle_error(e)

# ---------------------------------------------------------------------------
# Tool: campaign-level insights
# ---------------------------------------------------------------------------

@mcp.tool(name="meta_ads_get_campaign_insights", annotations={"title": "Meta Ads — Campaign Breakdown", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def meta_ads_get_campaign_insights(params: CampaignInsightsInput) -> str:
    """Get per-campaign performance for a Meta ad account.
    Returns one row per campaign: name, status, spend, CPM, CTR, purchases,
    revenue, ROAS, CPA, and a scale/hold/cut verdict. Sorted by spend desc.
    """
    try:
        date_p = _date_params(params.date_preset, params.start_date, params.end_date)
        raw    = await _graph(f"{params.account_id}/insights", {"fields": INSIGHT_FIELDS + ",campaign_id,campaign_name", "level": "campaign", **date_p, "limit": 100})
        campaigns = []
        for row in raw.get("data", []):
            fmt = _format_insights_row(row)
            if params.status_filter and params.status_filter != "ALL":
                if row.get("campaign_status") != params.status_filter:
                    continue
            campaigns.append({
                "campaign_id":   row.get("campaign_id"),
                "campaign_name": row.get("campaign_name"),
                "metrics":       fmt,
                "verdict":       "scale" if fmt["roas"] >= 2.0 else "hold" if fmt["roas"] >= 1.0 else "cut" if fmt["spend"] > 0 else "insufficient_data",
            })
        campaigns.sort(key=lambda c: c["metrics"]["spend"], reverse=True)
        return json.dumps({"account_id": params.account_id, "account_name": HAPPIE_ACCOUNTS.get(params.account_id, params.account_id), "period": date_p, "campaign_count": len(campaigns), "campaigns": campaigns}, indent=2)
    except Exception as e:
        return _handle_error(e)


# ---------------------------------------------------------------------------
# Tool: top ads
# ---------------------------------------------------------------------------

@mcp.tool(name="meta_ads_get_top_ads", annotations={"title": "Meta Ads — Top Ads by Metric", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def meta_ads_get_top_ads(params: TopAdsInput) -> str:
    """Get the top-performing ads ranked by link_ctr, spend, roas, purchases, or impressions.
    Use for creative performance reviews and identifying which ads to scale or retire.
    """
    try:
        date_p = _date_params(params.date_preset, params.start_date, params.end_date)
        raw    = await _graph(f"{params.account_id}/insights", {"fields": AD_INSIGHT_FIELDS, "level": "ad", **date_p, "limit": 200})
        sort_key = {"link_ctr": "link_ctr", "spend": "spend", "roas": "roas", "purchases": "purchases", "impressions": "impressions"}[params.sort_by.value]
        ads = []
        for row in raw.get("data", []):
            fmt = _format_insights_row(row)
            if fmt["impressions"] < 10:
                continue
            ads.append({"ad_id": row.get("ad_id"), "ad_name": row.get("ad_name"), "adset_name": row.get("adset_name"), "campaign_name": row.get("campaign_name"), "metrics": fmt})
        ads.sort(key=lambda a: a["metrics"][sort_key], reverse=True)
        return json.dumps({"account_id": params.account_id, "account_name": HAPPIE_ACCOUNTS.get(params.account_id, params.account_id), "period": date_p, "sort_by": params.sort_by.value, "returned": len(ads[:params.limit]), "top_ads": ads[:params.limit]}, indent=2)
    except Exception as e:
        return _handle_error(e)

# ---------------------------------------------------------------------------
# Tool: list campaigns
# ---------------------------------------------------------------------------

@mcp.tool(name="meta_ads_list_campaigns", annotations={"title": "Meta Ads — List Campaigns", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def meta_ads_list_campaigns(account_id: str, status: str = "ACTIVE") -> str:
    """List campaigns for a Meta ad account with budget and status.
    Returns campaign IDs, names, objectives, status, and daily/lifetime budgets.

    Args:
        account_id: Meta ad account ID (act_ prefix added automatically if omitted).
        status: Filter — ACTIVE, PAUSED, ARCHIVED, or ALL (default: ACTIVE).
    """
    try:
        if not account_id.startswith("act_"):
            account_id = f"act_{account_id}"
        effective = [status] if status != "ALL" else ["ACTIVE", "PAUSED", "ARCHIVED", "DELETED"]
        raw = await _graph(f"{account_id}/campaigns", {"fields": "id,name,objective,status,effective_status,daily_budget,lifetime_budget,start_time,stop_time", "effective_status": json.dumps(effective), "limit": 100})
        campaigns = [{"id": c.get("id"), "name": c.get("name"), "objective": c.get("objective"), "status": c.get("effective_status", c.get("status")), "daily_budget_usd": round(int(c["daily_budget"]) / 100, 2) if c.get("daily_budget") else None, "lifetime_budget_usd": round(int(c["lifetime_budget"]) / 100, 2) if c.get("lifetime_budget") else None, "start_time": c.get("start_time"), "stop_time": c.get("stop_time")} for c in raw.get("data", [])]
        return json.dumps({"account_id": account_id, "account_name": HAPPIE_ACCOUNTS.get(account_id, account_id), "status_filter": status, "count": len(campaigns), "campaigns": campaigns}, indent=2)
    except Exception as e:
        return _handle_error(e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    import uvicorn; uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=port)
