#!/usr/bin/env python3
"""
Meta Ads MCP Server — Happie Beverages
FastMCP + BearerTokenMiddleware, deployed on Railway.
Connected to Claude Desktop via mcp-remote (same pattern as multi-brand-mcp).

Required env vars:
  META_ACCESS_TOKEN  — non-expiring System User token (ads_read + read_insights)
  MCP_BEARER_TOKEN   — pre-shared token; mcp-remote sends it as Authorization: Bearer
Optional:
  META_API_VERSION   — default v21.0
  PORT               — default 8000
"""

import json, logging, os, secrets
from typing import Optional
from enum import Enum

import httpx, uvicorn
from pydantic import BaseModel, Field, field_validator, ConfigDict
from mcp.server.fastmcp import FastMCP
from starlette.types import ASGIApp, Receive, Scope, Send

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_VERSION = os.environ.get("META_API_VERSION", "v21.0")
GRAPH_BASE  = f"https://graph.facebook.com/{API_VERSION}"
META_TOKEN  = os.environ.get("META_ACCESS_TOKEN", "")
BEARER      = os.environ.get("MCP_BEARER_TOKEN", "")
PORT        = int(os.environ.get("PORT", 8000))

HAPPIE_ACCOUNTS = {
    "act_4265171330413775": "Happie Ads (Alec — brand/warmup)",
    "act_1473338457823788": "Happie Fusion (Muhammad — FF acquisition)",
}
INSIGHT_FIELDS = ",".join(["spend","impressions","reach","clicks","cpm","ctr",
    "inline_link_clicks","inline_link_click_ctr","cpc",
    "actions","action_values","cost_per_action_type","website_purchase_roas"])
AD_INSIGHT_FIELDS = ",".join(["ad_id","ad_name","adset_name","campaign_name",
    "spend","impressions","inline_link_click_ctr","inline_link_clicks",
    "cpm","actions","action_values","website_purchase_roas"])

# ---------------------------------------------------------------------------
# Bearer-token ASGI middleware (same pattern as multi-brand-mcp)
# ---------------------------------------------------------------------------
_PUBLIC = ["/.well-known/", "/health"]

async def _send_401(send: Send, reason: str) -> None:
    body = json.dumps({"error": "unauthorized", "reason": reason}).encode()
    await send({"type":"http.response.start","status":401,
                "headers":[[b"content-type",b"application/json"],
                            [b"content-length",str(len(body)).encode()],
                            [b"www-authenticate",b"Bearer"]]})
    await send({"type":"http.response.body","body":body,"more_body":False})

class BearerTokenMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app
        if not BEARER:
            logger.warning("MCP_BEARER_TOKEN not set — open (dev only)")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not BEARER:
            await self._app(scope, receive, send); return
        path = scope.get("path","")
        if any(path.startswith(p) for p in _PUBLIC):
            await self._app(scope, receive, send); return
        headers = dict(scope.get("headers",[]))
        auth = headers.get(b"authorization",b"").decode()
        if not auth.lower().startswith("bearer "):
            await _send_401(send, "missing_bearer"); return
        if not secrets.compare_digest(auth[7:].strip(), BEARER):
            await _send_401(send, "invalid_bearer"); return
        await self._app(scope, receive, send)

# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------
mcp = FastMCP("meta_ads_mcp")

# ---------------------------------------------------------------------------
# Graph API helpers
# ---------------------------------------------------------------------------
async def _graph(path: str, params: dict) -> dict:
    params["access_token"] = META_TOKEN
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(f"{GRAPH_BASE}/{path}", params=params)
        r.raise_for_status(); return r.json()

def _ea(items, t, d=0.0):
    for i in (items or []):
        if i.get("action_type") == t: return float(i.get("value",d))
    return d

def _roas(lst): return round(sum(float(r.get("value",0)) for r in (lst or [])),2)

def _fmt(row):
    a=row.get("actions") or []; v=row.get("action_values") or []
    cp=row.get("cost_per_action_type") or []; rl=row.get("website_purchase_roas") or []
    p=_ea(a,"offsite_conversion.fb_pixel_purchase") or _ea(a,"purchase")
    rev=_ea(v,"offsite_conversion.fb_pixel_purchase") or _ea(v,"purchase")
    cpa=_ea(cp,"offsite_conversion.fb_pixel_purchase") or _ea(cp,"purchase")
    return {"spend":round(float(row.get("spend",0)),2),
            "impressions":int(row.get("impressions",0)),
            "reach":int(row.get("reach",0)),
            "link_clicks":int(row.get("inline_link_clicks",0)),
            "cpm":round(float(row.get("cpm",0)),2),
            "link_ctr":round(float(row.get("inline_link_click_ctr",0)),4),
            "cpc":round(float(row.get("cpc",0)),2),
            "purchases":int(p),"revenue":round(rev,2),"cpa":round(cpa,2),"roas":_roas(rl)}

def _err(e):
    if isinstance(e, httpx.HTTPStatusError):
        try:
            b=e.response.json(); er=b.get("error",{})
            c=er.get("code",e.response.status_code); m=er.get("message",str(e))
            if c in (190,102): return f"Error: Meta token expired (code {c}). Refresh META_ACCESS_TOKEN."
            return f"Error {c}: {m}"
        except: return f"Error: HTTP {e.response.status_code}"
    if isinstance(e,httpx.TimeoutException): return "Error: timed out"
    return f"Error: {type(e).__name__}: {e}"

def _dp(preset, start, end):
    if start and end: return {"time_range":json.dumps({"since":start,"until":end})}
    return {"date_preset":preset.value}

def _norm(v): return v if v.startswith("act_") else f"act_{v}"

# ---------------------------------------------------------------------------
# Enums + Input models
# ---------------------------------------------------------------------------
class DatePreset(str, Enum):
    LAST_7D="last_7d"; LAST_14D="last_14d"; LAST_30D="last_30d"
    THIS_MONTH="this_month"; LAST_MONTH="last_month"; YESTERDAY="yesterday"

class SortBy(str, Enum):
    LINK_CTR="link_ctr"; SPEND="spend"; ROAS="roas"; PURCHASES="purchases"; IMPRESSIONS="impressions"

class AccountInsightsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    account_id: str = Field(..., description="Meta ad account ID. Happie defaults: act_4265171330413775 (Happie Ads) or act_1473338457823788 (Happie Fusion).")
    date_preset: DatePreset = Field(DatePreset.LAST_30D)
    start_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    @field_validator("account_id")
    @classmethod
    def normalize(cls, v: str) -> str: return _norm(v)

class CampaignInsightsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    account_id: str = Field(...)
    date_preset: DatePreset = Field(DatePreset.LAST_30D)
    start_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    status_filter: Optional[str] = Field(None)
    @field_validator("account_id")
    @classmethod
    def normalize(cls, v: str) -> str: return _norm(v)

class TopAdsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    account_id: str = Field(...)
    date_preset: DatePreset = Field(DatePreset.LAST_30D)
    sort_by: SortBy = Field(SortBy.LINK_CTR)
    limit: int = Field(5, ge=1, le=20)
    start_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    @field_validator("account_id")
    @classmethod
    def normalize(cls, v: str) -> str: return _norm(v)

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool(name="meta_ads_auth_check", annotations={"readOnlyHint":True,"destructiveHint":False})
async def meta_ads_auth_check() -> str:
    """Verify META_ACCESS_TOKEN is valid and list accessible ad accounts."""
    try:
        me = await _graph("me",{"fields":"id,name"})
        accts = await _graph("me/adaccounts",{"fields":"id,name,account_status,currency"})
        return json.dumps({"token_status":"valid","user":me,"ad_accounts":accts.get("data",[]),"happie_accounts":HAPPIE_ACCOUNTS},indent=2)
    except Exception as e: return _err(e)

@mcp.tool(name="meta_ads_get_account_insights", annotations={"readOnlyHint":True,"destructiveHint":False})
async def meta_ads_get_account_insights(params: AccountInsightsInput) -> str:
    """Get aggregate performance for a Meta ad account: spend, ROAS, CPA, CPM, CTR, purchases, revenue."""
    try:
        dp = _dp(params.date_preset, params.start_date, params.end_date)
        raw = await _graph(f"{params.account_id}/insights",{"fields":INSIGHT_FIELDS,"level":"account",**dp})
        data = raw.get("data",[])
        if not data: return json.dumps({"account_id":params.account_id,"period":dp,"summary":"No data — no active ads in this period."},indent=2)
        row = _fmt(data[0])
        v = "scale" if row["roas"]>=2 else "hold" if row["roas"]>=1 else "cut" if row["spend"]>0 else "insufficient_data"
        return json.dumps({"account_id":params.account_id,"account_name":HAPPIE_ACCOUNTS.get(params.account_id,params.account_id),"period":dp,"summary":row,"verdict":v},indent=2)
    except Exception as e: return _err(e)

@mcp.tool(name="meta_ads_get_campaign_insights", annotations={"readOnlyHint":True,"destructiveHint":False})
async def meta_ads_get_campaign_insights(params: CampaignInsightsInput) -> str:
    """Per-campaign breakdown: spend, ROAS, CPA, verdict (scale/hold/cut), sorted by spend desc."""
    try:
        dp = _dp(params.date_preset, params.start_date, params.end_date)
        raw = await _graph(f"{params.account_id}/insights",{"fields":INSIGHT_FIELDS+",campaign_id,campaign_name","level":"campaign",**dp,"limit":100})
        camps=[]
        for row in raw.get("data",[]):
            f=_fmt(row)
            if params.status_filter and params.status_filter!="ALL" and row.get("campaign_status")!=params.status_filter: continue
            camps.append({"campaign_id":row.get("campaign_id"),"campaign_name":row.get("campaign_name"),"metrics":f,"verdict":"scale" if f["roas"]>=2 else "hold" if f["roas"]>=1 else "cut" if f["spend"]>0 else "insufficient_data"})
        camps.sort(key=lambda c:c["metrics"]["spend"],reverse=True)
        return json.dumps({"account_id":params.account_id,"account_name":HAPPIE_ACCOUNTS.get(params.account_id,params.account_id),"period":dp,"campaigns":camps},indent=2)
    except Exception as e: return _err(e)

@mcp.tool(name="meta_ads_get_top_ads", annotations={"readOnlyHint":True,"destructiveHint":False})
async def meta_ads_get_top_ads(params: TopAdsInput) -> str:
    """Top N ads ranked by link_ctr, spend, roas, purchases, or impressions."""
    try:
        dp = _dp(params.date_preset, params.start_date, params.end_date)
        raw = await _graph(f"{params.account_id}/insights",{"fields":AD_INSIGHT_FIELDS,"level":"ad",**dp,"limit":200})
        ads=[{"ad_id":r.get("ad_id"),"ad_name":r.get("ad_name"),"adset_name":r.get("adset_name"),"campaign_name":r.get("campaign_name"),"metrics":_fmt(r)} for r in raw.get("data",[]) if int(r.get("impressions",0))>=10]
        ads.sort(key=lambda a:a["metrics"][params.sort_by.value],reverse=True)
        return json.dumps({"account_id":params.account_id,"account_name":HAPPIE_ACCOUNTS.get(params.account_id,params.account_id),"period":dp,"sort_by":params.sort_by.value,"top_ads":ads[:params.limit]},indent=2)
    except Exception as e: return _err(e)

@mcp.tool(name="meta_ads_list_campaigns", annotations={"readOnlyHint":True,"destructiveHint":False})
async def meta_ads_list_campaigns(account_id: str, status: str = "ACTIVE") -> str:
    """List campaigns with budget, objective, status. status: ACTIVE | PAUSED | ARCHIVED | ALL."""
    try:
        if not account_id.startswith("act_"): account_id=f"act_{account_id}"
        eff=[status] if status!="ALL" else ["ACTIVE","PAUSED","ARCHIVED","DELETED"]
        raw=await _graph(f"{account_id}/campaigns",{"fields":"id,name,objective,status,effective_status,daily_budget,lifetime_budget","effective_status":json.dumps(eff),"limit":100})
        camps=[{"id":c.get("id"),"name":c.get("name"),"objective":c.get("objective"),"status":c.get("effective_status"),"daily_budget_usd":round(int(c["daily_budget"])/100,2) if c.get("daily_budget") else None} for c in raw.get("data",[])]
        return json.dumps({"account_id":account_id,"account_name":HAPPIE_ACCOUNTS.get(account_id,account_id),"count":len(camps),"campaigns":camps},indent=2)
    except Exception as e: return _err(e)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("Starting meta_ads_mcp on port %d", PORT)
    app = BearerTokenMiddleware(mcp.streamable_http_app())
    uvicorn.run(app, host="0.0.0.0", port=PORT)
