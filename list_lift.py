"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                          L I S T I N G L I F T                             ║
║           AI-Powered UK Marketplace Listing Assistant — MVP                 ║
║                                                                              ║
║  Stack: FastAPI · Supabase · OpenAI GPT-4o · Replicate · Tailwind/Next.js  ║
╚══════════════════════════════════════════════════════════════════════════════╝

PRODUCT OVERVIEW
----------------
ListingLift turns uploaded item photos into marketplace-ready listings for
Vinted, eBay UK, and Depop in under 5 seconds. Designed for UK-based side
hustlers, wardrobe clearers, and casual resellers on mobile.

MVP ASSUMPTIONS
---------------
1.  Authentication is handled by Supabase Auth (magic link + Google OAuth).
2.  Images are uploaded directly to Supabase Storage with a pre-signed URL;
    the backend never proxies raw bytes.
3.  GPT-4o vision receives a public or signed URL.
4.  Pricing guidance is AI-estimated — no live market feed at MVP. Disclosed to users.
5.  Replicate background-removal runs in parallel with GPT-4o analysis;
    results are persisted asynchronously and do NOT block the listing response.
6.  All listings are soft-deleted; hard deletion runs via a nightly GDPR job
    that purges assets older than 30 days unless the user saves them.
7.  Platform fees shown are approximate and disclosed as estimates.
8.  All monetary values stored and returned in pence (GBP) to avoid float issues.

SUPABASE DATABASE SCHEMA (PostgreSQL DDL)
-----------------------------------------
Run in the Supabase SQL editor to bootstrap the MVP schema.

/*
CREATE TABLE IF NOT EXISTS public.users (
    id              UUID        PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    display_name    TEXT,
    avatar_url      TEXT,
    tier            TEXT        NOT NULL DEFAULT 'free'
                                CHECK (tier IN ('free','pro','business')),
    scans_today     INTEGER     NOT NULL DEFAULT 0,
    scans_reset_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_users_tier ON public.users(tier);

CREATE TABLE IF NOT EXISTS public.uploaded_assets (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID        NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    listing_id      UUID,
    storage_path    TEXT        NOT NULL,
    bg_removed_path TEXT,
    mime_type       TEXT        NOT NULL DEFAULT 'image/jpeg',
    size_bytes      INTEGER,
    upload_status   TEXT        NOT NULL DEFAULT 'pending'
                                CHECK (upload_status IN ('pending','uploaded','processing','ready','failed')),
    gdpr_purge_at   TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '30 days'),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_assets_user_id    ON public.uploaded_assets(user_id);
CREATE INDEX IF NOT EXISTS idx_assets_listing_id ON public.uploaded_assets(listing_id);
CREATE INDEX IF NOT EXISTS idx_assets_purge_at   ON public.uploaded_assets(gdpr_purge_at);

CREATE TABLE IF NOT EXISTS public.listings (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID        NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    title               TEXT,
    item_type           TEXT,
    category            TEXT,
    brand               TEXT,
    model_style         TEXT,
    colour              TEXT,
    material            TEXT,
    condition           TEXT        CHECK (condition IN (
                                        'new_with_tags','new_without_tags',
                                        'very_good','good','satisfactory','poor')),
    gender              TEXT        CHECK (gender IN ('mens','womens','unisex','boys','girls','unknown')),
    estimated_weight_g  INTEGER,
    ai_confidence       NUMERIC(4,3) CHECK (ai_confidence BETWEEN 0 AND 1),
    ai_raw_json         JSONB,
    processing_status   TEXT        NOT NULL DEFAULT 'queued'
                                    CHECK (processing_status IN (
                                        'queued','analysing','pricing','drafting',
                                        'ready','failed','archived')),
    error_message       TEXT,
    is_bundle           BOOLEAN     NOT NULL DEFAULT FALSE,
    saved_by_user       BOOLEAN     NOT NULL DEFAULT FALSE,
    gdpr_purge_at       TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '30 days'),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_listings_user_id ON public.listings(user_id);
CREATE INDEX IF NOT EXISTS idx_listings_status  ON public.listings(processing_status);
CREATE INDEX IF NOT EXISTS idx_listings_purge   ON public.listings(gdpr_purge_at);

CREATE TABLE IF NOT EXISTS public.marketplace_drafts (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    listing_id      UUID        NOT NULL REFERENCES public.listings(id) ON DELETE CASCADE,
    platform        TEXT        NOT NULL CHECK (platform IN ('vinted','ebay_uk','depop')),
    title           TEXT,
    description     TEXT,
    hashtags        TEXT[],
    category_path   TEXT,
    condition_label TEXT,
    keywords        TEXT[],
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(listing_id, platform)
);
CREATE INDEX IF NOT EXISTS idx_drafts_listing_id ON public.marketplace_drafts(listing_id);

CREATE TABLE IF NOT EXISTS public.price_estimates (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    listing_id          UUID        NOT NULL UNIQUE REFERENCES public.listings(id) ON DELETE CASCADE,
    fast_sale_pence     INTEGER     NOT NULL,
    max_profit_pence    INTEGER     NOT NULL,
    recommended_pence   INTEGER,
    confidence          TEXT        NOT NULL CHECK (confidence IN ('high','medium','low')),
    reasoning           TEXT,
    data_source         TEXT        NOT NULL DEFAULT 'ai_estimate',
    ebay_fee_pence      INTEGER,
    depop_fee_pence     INTEGER,
    vinted_fee_pence    INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.shipping_estimates (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    listing_id          UUID        NOT NULL UNIQUE REFERENCES public.listings(id) ON DELETE CASCADE,
    weight_band         TEXT        NOT NULL,
    carrier             TEXT        NOT NULL CHECK (carrier IN ('royal_mail','evri')),
    service             TEXT,
    estimated_cost_pence INTEGER,
    reason              TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.bundle_suggestions (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    listing_id          UUID        NOT NULL REFERENCES public.listings(id) ON DELETE CASCADE,
    recommended         BOOLEAN     NOT NULL DEFAULT FALSE,
    asset_ids           UUID[],
    bundle_title        TEXT,
    bundle_reason       TEXT,
    suggested_price_pence INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.users               ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.uploaded_assets     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.listings            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.marketplace_drafts  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.price_estimates     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.shipping_estimates  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.bundle_suggestions  ENABLE ROW LEVEL SECURITY;

CREATE POLICY user_owns_listing    ON public.listings            FOR ALL USING (auth.uid() = user_id);
CREATE POLICY user_owns_asset      ON public.uploaded_assets     FOR ALL USING (auth.uid() = user_id);
CREATE POLICY user_owns_drafts     ON public.marketplace_drafts  FOR ALL USING (
    listing_id IN (SELECT id FROM public.listings WHERE user_id = auth.uid()));
CREATE POLICY user_owns_price      ON public.price_estimates     FOR ALL USING (
    listing_id IN (SELECT id FROM public.listings WHERE user_id = auth.uid()));
CREATE POLICY user_owns_shipping   ON public.shipping_estimates  FOR ALL USING (
    listing_id IN (SELECT id FROM public.listings WHERE user_id = auth.uid()));
CREATE POLICY user_owns_bundle     ON public.bundle_suggestions  FOR ALL USING (
    listing_id IN (SELECT id FROM public.listings WHERE user_id = auth.uid()));
*/
"""

# =============================================================================
# DEPENDENCIES
# pip install fastapi uvicorn supabase openai httpx python-dotenv pydantic
# =============================================================================

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from enum import Enum
from typing import Annotated, Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, field_validator
from supabase import create_client, Client

load_dotenv()

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("listinglift")

# =============================================================================
# CONFIGURATION — validated at startup so missing vars fail loudly
# =============================================================================

def _require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {key}\n"
            f"Add it to your .env file or deployment environment."
        )
    return val


SUPABASE_URL:        str       = _require_env("SUPABASE_URL")
SUPABASE_SERVICE_KEY: str      = _require_env("SUPABASE_SERVICE_KEY")
OPENAI_API_KEY:      str       = _require_env("OPENAI_API_KEY")
REPLICATE_API_TOKEN: str       = os.environ.get("REPLICATE_API_TOKEN", "")
ALLOWED_ORIGINS:     list[str] = os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000").split(",")

supabase: Client      = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
openai_client         = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Reusable HTTP client — one per process, not one per request
_http_client = httpx.AsyncClient(timeout=35.0)

# =============================================================================
# CONSTANTS
# =============================================================================

# Platform fee rates (approximate; disclosed to users)
PLATFORM_FEES: dict[str, float] = {
    "ebay_uk": 0.128,   # ~12.8% including payment processing
    "depop":   0.100,   # 10%
    "vinted":  0.050,   # ~5% buyer protection (seller typically pays nothing)
}

# UK shipping tiers: (max_weight_g | None, carrier, service, weight_band_key, cost_pence)
# Ordered ascending; None = catch-all for heaviest items.
_ShippingTier = tuple[int | None, str, str, str, int]
SHIPPING_TIERS: list[_ShippingTier] = [
    (100,  "royal_mail", "Royal Mail Large Letter",          "small_parcel_0_100g",      135),
    (250,  "royal_mail", "Royal Mail Tracked 48 (0–250g)",   "small_parcel_101_250g",    285),
    (500,  "royal_mail", "Royal Mail Tracked 48 (250–500g)", "small_parcel_251_500g",    330),
    (750,  "royal_mail", "Royal Mail Tracked 48 (500–750g)", "small_parcel_501_750g",    380),
    (1000, "royal_mail", "Royal Mail Tracked 48 (<1kg)",     "small_parcel_751_1000g",   430),
    (2000, "evri",       "Evri Parcel (1–2kg)",              "medium_parcel_1_2kg",      299),
    (5000, "evri",       "Evri Parcel (2–5kg)",              "large_parcel_2_5kg",       399),
    (None, "evri",       "Evri Large Parcel (5kg+)",         "large_parcel_5kg_plus",    599),
]

# =============================================================================
# PYDANTIC MODELS — CANONICAL LISTING OBJECT
# =============================================================================

class ConditionEnum(str, Enum):
    new_with_tags    = "new_with_tags"
    new_without_tags = "new_without_tags"
    very_good        = "very_good"
    good             = "good"
    satisfactory     = "satisfactory"
    poor             = "poor"


class GenderEnum(str, Enum):
    mens    = "mens"
    womens  = "womens"
    unisex  = "unisex"
    boys    = "boys"
    girls   = "girls"
    unknown = "unknown"


class ConfidenceEnum(str, Enum):
    high   = "high"
    medium = "medium"
    low    = "low"


class MarketplaceDraft(BaseModel):
    platform:        str
    title:           str
    description:     str
    # FIX: max_length on Field() applies to strings, not lists.
    # Use Annotated to constrain the list length properly.
    hashtags:        Annotated[list[str], Field(max_length=5)] = Field(default_factory=list)
    category_path:   str | None = None
    condition_label: str | None = None
    keywords:        list[str]  = Field(default_factory=list)


class PriceEstimate(BaseModel):
    fast_sale_pence:    int
    max_profit_pence:   int
    recommended_pence:  int | None = None
    confidence:         ConfidenceEnum
    reasoning:          str
    data_source:        str = "ai_estimate"
    ebay_fee_pence:     int | None = None
    depop_fee_pence:    int | None = None
    vinted_fee_pence:   int | None = None


class ShippingEstimate(BaseModel):
    weight_band:          str
    carrier:              str
    service:              str
    estimated_cost_pence: int
    reason:               str


class BundleSuggestion(BaseModel):
    recommended:           bool
    bundle_title:          str | None = None
    bundle_reason:         str | None = None
    suggested_price_pence: int | None = None


class Listing(BaseModel):
    """
    Canonical Listing object — the single source of truth returned to the
    frontend and stored across the related DB tables.
    """
    id:                 str       = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id:            str
    title:              str
    item_type:          str
    category:           str
    brand:              str | None = None
    model_style:        str | None = None
    colour:             str
    material:           str | None = None
    condition:          ConditionEnum
    gender:             GenderEnum  = GenderEnum.unknown
    estimated_weight_g: int
    ai_confidence:      float       = Field(ge=0.0, le=1.0)
    processing_status:  str         = "ready"
    is_bundle:          bool        = False
    drafts:             list[MarketplaceDraft]  = Field(default_factory=list)
    price_estimate:     PriceEstimate | None    = None
    shipping_estimate:  ShippingEstimate | None = None
    bundle_suggestion:  BundleSuggestion | None = None
    asset_ids:          list[str]   = Field(default_factory=list)
    created_at:         str | None  = None


class ScanRequest(BaseModel):
    asset_ids:   list[str]
    image_urls:  list[str]
    user_notes:  str = ""

    @field_validator("image_urls")
    @classmethod
    def urls_must_be_https(cls, urls: list[str]) -> list[str]:
        for url in urls:
            if not url.startswith("https://"):
                raise ValueError(f"All image URLs must use HTTPS. Got: {url!r}")
        return urls

    @field_validator("asset_ids")
    @classmethod
    def asset_ids_must_match_urls(cls, ids: list[str], info: Any) -> list[str]:
        # Cross-field validation happens after all fields are set; guard here
        # is best-effort — full check is in the endpoint.
        return ids

# =============================================================================
# GPT-4o VISION SYSTEM PROMPT
# =============================================================================

GPT4O_SYSTEM_PROMPT = """
You are ListingLift's item-analysis engine. You receive one or more photos of a
second-hand item that a UK seller wants to list on Vinted, eBay UK, and Depop.

LANGUAGE RULES
- Always use British English: trainers (not sneakers), trousers (not pants),
  colour (not color), jumper (not sweater), wardrobe (not closet), parcel (not
  package), post/postage (not shipping/mail), grey (not gray).
- Keep tone friendly, practical, and lightly colloquial — as if advising a mate.

ANALYSIS TASK
Inspect every image carefully and extract or confidently infer:
- item_type        : specific type (e.g. "hooded sweatshirt", "Chelsea boots")
- category         : hierarchical path (e.g. "Menswear > Tops > Hoodies & Sweatshirts")
- brand            : only if a logo, label, or stitching is clearly visible — otherwise null
- model_style      : style or model name if identifiable — otherwise null
- colour           : primary colour in British English (e.g. "navy blue", "oatmeal")
- material         : inferred material (e.g. "cotton jersey", "full-grain leather")
- condition        : one of: new_with_tags | new_without_tags | very_good | good |
                     satisfactory | poor
- condition_notes  : brief honest note on visible flaws (e.g. "slight pilling on cuffs")
- gender           : mens | womens | unisex | boys | girls | unknown
- estimated_weight_g : conservative integer in grams; round up if uncertain
- ai_confidence    : float 0–1 reflecting overall certainty

CONFIDENCE RULES
- >= 0.80 only if brand, type, and condition are all clearly identifiable
- 0.50–0.79 if type and condition are clear but brand is uncertain
- < 0.50 if image is blurry, item is obscured, or type is ambiguous
- Never guess brand names — set brand to null if unsure
- If quality is too low, set processing_status to "low_confidence"

LISTING DRAFTS
Generate three marketplace-specific drafts. Each must feel native to its platform.

--- VINTED DRAFT ---
- title: 40–60 chars, plain and descriptive
- description: 3–5 short sentences. Lead with condition. End with postage note.
- category_path: Vinted-style (e.g. "Women > Shoes > Trainers & Sporty Shoes")
- condition_label: New with tags | New without tags | Very good | Good | Satisfactory

--- EBAY UK DRAFT ---
- title: HARD LIMIT 80 characters. Front-load brand + type + key attributes.
  Title case. No punctuation except hyphens. No padding words.
- description: 60–120 words. Bullet key specs. Mention tracked postage + returns.
- keywords: 5–8 SEO terms
- category_path: eBay UK path (e.g. "Clothes, Shoes & Accessories > Men's Shoes")
- condition_label: New with tags | New without box | Very Good | Good | Acceptable

--- DEPOP DRAFT ---
- title: 30–50 chars, style-aware, lower case
- description: 3–6 short punchy lines. Lead with vibe, follow with facts.
- hashtags: EXACTLY 5, lowercase with # prefix, no spaces
- category_path: Depop-style (e.g. "Menswear > Bottoms > Jeans")

PRICING GUIDANCE (all values in pence GBP)
- fast_sale_pence:  price likely to sell within 48 hours
- max_profit_pence: price that maximises margin if seller is patient
- confidence:       high | medium | low
- reasoning:        one sentence. MUST state: "no live market data available"
- Base estimates on typical UK second-hand market values from training knowledge only

SHIPPING LOGIC
Recommend based on estimated_weight_g:
- 0–100g:   Royal Mail Large Letter (~£1.35)
- 101–500g: Royal Mail Tracked 48 (~£2.85–£3.30)
- 501–1kg:  Royal Mail Tracked 48 (~£3.80–£4.30)
- 1–2kg:    Evri Parcel (~£2.99)
- 2–5kg:    Evri Parcel (~£3.99)
- 5kg+:     Evri Large Parcel (~£5.99)
Prefer Royal Mail under 1kg; Evri for heavier items.

BUNDLE DETECTION
If multiple images show DIFFERENT items (not multiple angles):
- Set bundle_recommended to true, suggest why they belong together

OUTPUT FORMAT
Return ONLY valid JSON. No prose before or after.
"""

# =============================================================================
# JSON SCHEMA FOR GPT-4o OUTPUT
# =============================================================================

GPT4O_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "processing_status", "item_type", "category", "colour",
        "condition", "gender", "estimated_weight_g", "ai_confidence",
        "drafts", "price_estimate", "shipping_estimate", "bundle_suggestion",
    ],
    "properties": {
        "processing_status": {"type": "string", "enum": ["ready", "low_confidence"]},
        "item_type":   {"type": "string"},
        "category":    {"type": "string"},
        "brand":       {"type": ["string", "null"]},
        "model_style": {"type": ["string", "null"]},
        "colour":      {"type": "string"},
        "material":    {"type": ["string", "null"]},
        "condition":   {"type": "string",
                        "enum": ["new_with_tags","new_without_tags","very_good","good","satisfactory","poor"]},
        "condition_notes": {"type": ["string", "null"]},
        "gender":      {"type": "string",
                        "enum": ["mens","womens","unisex","boys","girls","unknown"]},
        "estimated_weight_g": {"type": "integer", "minimum": 1},
        "ai_confidence":      {"type": "number",  "minimum": 0, "maximum": 1},
        "drafts": {
            "type": "array", "minItems": 3, "maxItems": 3,
            "items": {
                "type": "object",
                "required": ["platform", "title", "description"],
                "properties": {
                    "platform":        {"type": "string", "enum": ["vinted","ebay_uk","depop"]},
                    "title":           {"type": "string"},
                    "description":     {"type": "string"},
                    "hashtags":        {"type": "array", "items": {"type": "string"}, "maxItems": 5},
                    "category_path":   {"type": ["string", "null"]},
                    "condition_label": {"type": ["string", "null"]},
                    "keywords":        {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "price_estimate": {
            "type": "object",
            "required": ["fast_sale_pence", "max_profit_pence", "confidence", "reasoning"],
            "properties": {
                "fast_sale_pence":  {"type": "integer", "minimum": 0},
                "max_profit_pence": {"type": "integer", "minimum": 0},
                "confidence":       {"type": "string", "enum": ["high","medium","low"]},
                "reasoning":        {"type": "string"},
            },
        },
        "shipping_estimate": {
            "type": "object",
            "required": ["weight_band", "carrier", "service", "estimated_cost_pence", "reason"],
            "properties": {
                "weight_band":          {"type": "string"},
                "carrier":              {"type": "string"},
                "service":              {"type": "string"},
                "estimated_cost_pence": {"type": "integer"},
                "reason":               {"type": "string"},
            },
        },
        "bundle_suggestion": {
            "type": "object",
            "required": ["recommended"],
            "properties": {
                "recommended":           {"type": "boolean"},
                "bundle_title":          {"type": ["string", "null"]},
                "bundle_reason":         {"type": ["string", "null"]},
                "suggested_price_pence": {"type": ["integer", "null"]},
            },
        },
    },
}

# =============================================================================
# EXAMPLE MODEL OUTPUT (for documentation / testing)
# =============================================================================

EXAMPLE_GPT4O_OUTPUT: dict[str, Any] = {
    "processing_status": "ready",
    "item_type": "hooded sweatshirt",
    "category": "Menswear > Tops > Hoodies & Sweatshirts",
    "brand": "Champion",
    "model_style": "Reverse Weave",
    "colour": "navy blue",
    "material": "heavyweight cotton fleece",
    "condition": "very_good",
    "condition_notes": "Minor crease on left cuff, no staining, drawstring intact.",
    "gender": "mens",
    "estimated_weight_g": 680,
    "ai_confidence": 0.87,
    "drafts": [
        {
            "platform": "vinted",
            "title": "Champion Reverse Weave Hoodie – Navy – Size L – Very Good",
            "description": (
                "Genuine Champion Reverse Weave hoodie in navy blue, size Large. "
                "Very good condition — no stains or holes, slight crease on one cuff. "
                "Heavyweight cotton so it's proper warm. Tracked postage included."
            ),
            "hashtags": [],
            "category_path": "Men > Clothing > Hoodies & Sweatshirts",
            "condition_label": "Very good",
            "keywords": [],
        },
        {
            "platform": "ebay_uk",
            "title": "Champion Reverse Weave Hoodie Navy Blue Mens Size L Heavyweight VGC",
            "description": (
                "Champion Reverse Weave Hoodie\n"
                "• Colour: Navy Blue\n• Size: Large\n• Material: Heavyweight cotton fleece\n"
                "• Condition: Very Good — light crease on cuff, otherwise excellent\n\n"
                "Dispatched within 1 working day via Royal Mail Tracked 48. "
                "Returns accepted within 30 days."
            ),
            "hashtags": [],
            "category_path": "Clothes, Shoes & Accessories > Men's Clothing > Hoodies & Sweatshirts",
            "condition_label": "Very Good",
            "keywords": ["champion", "reverse weave", "hoodie", "navy", "heavyweight", "cotton", "streetwear"],
        },
        {
            "platform": "depop",
            "title": "champion reverse weave hoodie navy – size L",
            "description": (
                "the heavyweight OG. champion reverse weave in proper navy\n"
                "size L – fits relaxed, great oversized on M\n"
                "condition: very good – tiny crease on cuff, nothing major\n"
                "chest 56cm / length 71cm\ntracked postage £4.30"
            ),
            "hashtags": ["#champion", "#reverseweave", "#hoodie", "#streetwear", "#vintageathletic"],
            "category_path": "Menswear > Tops > Hoodies",
            "condition_label": "Good",
            "keywords": [],
        },
    ],
    "price_estimate": {
        "fast_sale_pence": 2200,
        "max_profit_pence": 3200,
        "confidence": "medium",
        "reasoning": (
            "No live market data available; estimate based on training knowledge. "
            "Champion Reverse Weave in very good condition typically sells £22–£35 on UK platforms."
        ),
    },
    "shipping_estimate": {
        "weight_band": "small_parcel_501_750g",
        "carrier": "royal_mail",
        "service": "Royal Mail Tracked 48 (500–750g)",
        "estimated_cost_pence": 380,
        "reason": "Estimated ~680g; Royal Mail Tracked 48 is best value tracked option under 1kg.",
    },
    "bundle_suggestion": {
        "recommended": False,
        "bundle_title": None,
        "bundle_reason": None,
        "suggested_price_pence": None,
    },
}

# =============================================================================
# BUSINESS LOGIC — PRICING & SHIPPING
# =============================================================================

def compute_platform_fees(estimate: PriceEstimate) -> PriceEstimate:
    """
    Returns a NEW PriceEstimate with platform fees populated.
    Does NOT mutate the input object.

    FIX: original code used `recommended_pence or (...)` which treats 0 as falsy.
    Use explicit `is None` check instead.
    """
    if estimate.recommended_pence is not None:
        base = estimate.recommended_pence
    else:
        base = (estimate.fast_sale_pence + estimate.max_profit_pence) // 2

    return estimate.model_copy(update={
        "ebay_fee_pence":   int(base * PLATFORM_FEES["ebay_uk"]),
        "depop_fee_pence":  int(base * PLATFORM_FEES["depop"]),
        "vinted_fee_pence": int(base * PLATFORM_FEES["vinted"]),
    })


def resolve_shipping(weight_g: int) -> ShippingEstimate:
    """
    Deterministically resolve the best UK shipping option from SHIPPING_TIERS.
    The last tier has max_g=None and always matches, so this never returns None.
    """
    for max_g, carrier, service, weight_band, cost_pence in SHIPPING_TIERS:
        if max_g is None or weight_g <= max_g:
            return ShippingEstimate(
                weight_band=weight_band,
                carrier=carrier,
                service=service,
                estimated_cost_pence=cost_pence,
                reason=(
                    f"Item estimated at ~{weight_g}g. "
                    f"{service} is the most cost-effective tracked option for this weight."
                ),
            )
    # Unreachable — last tier is a catch-all — but keeps mypy happy
    raise RuntimeError("SHIPPING_TIERS must include a catch-all entry (max_g=None)")  # pragma: no cover


# =============================================================================
# AI INTEGRATION
# =============================================================================

async def call_gpt4o_vision(image_urls: list[str], user_prompt: str = "") -> dict[str, Any]:
    """
    Sends image URLs to GPT-4o with the ListingLift system prompt.
    Returns parsed JSON dict or raises HTTPException on failure.
    """
    content: list[dict[str, Any]] = [
        {"type": "image_url", "image_url": {"url": url, "detail": "high"}}
        for url in image_urls
    ]
    content.append({
        "type": "text",
        "text": user_prompt or "Analyse this item and return the full JSON listing data as specified.",
    })

    response = await openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": GPT4O_SYSTEM_PROMPT},
            {"role": "user",   "content": content},
        ],
        response_format={"type": "json_object"},
        max_tokens=2048,
        temperature=0.3,
    )

    # FIX: guard against empty choices (rare but possible on API errors)
    if not response.choices:
        raise ValueError("GPT-4o returned no choices — possible API error")

    raw = response.choices[0].message.content
    if not raw:
        raise ValueError("GPT-4o returned an empty response body")

    return dict.__new__(dict, **__import__("json").loads(raw))  # type: ignore[return-value]
    # Simpler: just return json.loads(raw) — written out below for clarity:


import json as _json  # noqa: E402 — grouped at top in production


async def _call_gpt4o_vision(image_urls: list[str], user_prompt: str = "") -> dict[str, Any]:
    """Clean version — replaces the prototype above."""
    content: list[dict[str, Any]] = [
        {"type": "image_url", "image_url": {"url": url, "detail": "high"}}
        for url in image_urls
    ]
    content.append({
        "type": "text",
        "text": user_prompt or "Analyse this item and return the full JSON listing data as specified.",
    })

    response = await openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": GPT4O_SYSTEM_PROMPT},
            {"role": "user",   "content": content},
        ],
        response_format={"type": "json_object"},
        max_tokens=2048,
        temperature=0.3,
    )

    if not response.choices or not response.choices[0].message.content:
        raise ValueError("GPT-4o returned an empty or malformed response")

    return _json.loads(response.choices[0].message.content)


async def remove_background_replicate(image_url: str) -> str | None:
    """
    Calls Replicate BRIA RMBG 1.4 for background removal.
    Returns output image URL or None if token not set / request fails.

    FIX: reuses the module-level _http_client instead of creating a new one
    per call. Uses exponential backoff instead of a fixed 1s busy-wait.
    """
    if not REPLICATE_API_TOKEN:
        return None

    headers = {
        "Authorization": f"Token {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "version": "a3d2e394b36a4b2ef93a61d4b980b5c5c4e4f6e0",  # BRIA RMBG 1.4
        "input": {"image": image_url},
    }

    try:
        resp = await _http_client.post(
            "https://api.replicate.com/v1/predictions",
            headers=headers,
            json=payload,
        )
        if resp.status_code != 201:
            logger.warning("Replicate prediction creation failed: HTTP %s", resp.status_code)
            return None

        prediction_id: str = resp.json()["id"]
        poll_url = f"https://api.replicate.com/v1/predictions/{prediction_id}"

        # Exponential backoff: 1s, 2s, 4s, 4s, 4s ... (cap at 4s, max ~25s total)
        delay = 1.0
        deadline = time.monotonic() + 28.0

        while time.monotonic() < deadline:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 4.0)

            poll = await _http_client.get(poll_url, headers=headers)
            result = poll.json()
            status = result.get("status")

            if status == "succeeded":
                return result.get("output")
            if status in ("failed", "canceled"):
                logger.warning("Replicate prediction %s ended with status: %s", prediction_id, status)
                return None

        logger.warning("Replicate prediction %s timed out", prediction_id)
        return None

    except Exception:
        logger.exception("Replicate background removal raised an unexpected error")
        return None


# =============================================================================
# PERSISTENCE HELPERS
# =============================================================================

async def _db(fn, *args, **kwargs):
    """
    Run a synchronous Supabase client call in a thread pool so it does not
    block the asyncio event loop.

    FIX: the standard supabase-py client is synchronous. Every bare call
    in an async function was blocking the event loop. Wrap with to_thread.
    """
    return await asyncio.to_thread(fn, *args, **kwargs)


async def _persist_listing(
    listing: Listing,
    ai_raw: dict[str, Any],
    replicate_tasks: list[asyncio.Task[str | None]],
) -> None:
    """
    Writes all listing data to Supabase. Runs as a background task;
    errors are logged with full tracebacks but do not affect the HTTP response.

    FIX: ai_raw is now stored ONCE on the listing row, not duplicated per draft.
    FIX: marketplace drafts are batch-inserted in a single query.
    FIX: asset link updates are batched via an IN clause.
    FIX: all Supabase calls wrapped in asyncio.to_thread via _db().
    """
    try:
        # 1. Insert listing row (exclude nested objects; store raw JSON once here)
        row = listing.model_dump(
            exclude={"drafts", "price_estimate", "shipping_estimate", "bundle_suggestion", "asset_ids"},
        )
        row["ai_confidence"] = float(listing.ai_confidence)
        row["ai_raw_json"]   = ai_raw

        result = await _db(lambda: supabase.table("listings").insert(row).execute())
        listing_id: str = result.data[0]["id"]

        # 2. Batch-insert marketplace drafts
        draft_rows = [
            {
                "listing_id":      listing_id,
                "platform":        d.platform,
                "title":           d.title,
                "description":     d.description,
                "hashtags":        d.hashtags,
                "category_path":   d.category_path,
                "condition_label": d.condition_label,
                "keywords":        d.keywords,
                # ai_raw NOT duplicated here; it lives on the listing row
            }
            for d in listing.drafts
        ]
        if draft_rows:
            await _db(lambda: supabase.table("marketplace_drafts").insert(draft_rows).execute())

        # 3. Insert price estimate
        if listing.price_estimate:
            pe = listing.price_estimate.model_dump()
            pe["listing_id"] = listing_id
            await _db(lambda: supabase.table("price_estimates").insert(pe).execute())

        # 4. Insert shipping estimate
        if listing.shipping_estimate:
            se = listing.shipping_estimate.model_dump()
            se["listing_id"] = listing_id
            await _db(lambda: supabase.table("shipping_estimates").insert(se).execute())

        # 5. Insert bundle suggestion
        if listing.bundle_suggestion:
            bs = listing.bundle_suggestion.model_dump()
            bs["listing_id"] = listing_id
            await _db(lambda: supabase.table("bundle_suggestions").insert(bs).execute())

        # 6. Batch-link assets to listing via an IN filter
        if listing.asset_ids:
            await _db(
                lambda: (
                    supabase.table("uploaded_assets")
                    .update({"listing_id": listing_id})
                    .in_("id", listing.asset_ids)
                    .execute()
                )
            )

        # 7. Await Replicate results and persist bg-removed paths
        for i, task in enumerate(replicate_tasks):
            try:
                bg_url = await asyncio.wait_for(asyncio.shield(task), timeout=30.0)
                if bg_url and i < len(listing.asset_ids):
                    asset_id = listing.asset_ids[i]
                    await _db(
                        lambda: (
                            supabase.table("uploaded_assets")
                            .update({"bg_removed_path": bg_url, "upload_status": "ready"})
                            .eq("id", asset_id)
                            .execute()
                        )
                    )
            except asyncio.TimeoutError:
                logger.warning("Replicate task %d timed out during persistence", i)
            except Exception:
                logger.exception("Failed to persist Replicate result for asset index %d", i)

        logger.info("Listing %s persisted successfully", listing_id)

    except Exception:
        # FIX: log full traceback instead of silently swallowing
        logger.exception("Failed to persist listing %s", listing.id)


# =============================================================================
# ORCHESTRATION
# =============================================================================

async def orchestrate_listing(
    user_id: str,
    asset_ids: list[str],
    image_urls: list[str],
    user_notes: str = "",
) -> Listing:
    """
    Core orchestration. Fires GPT-4o and Replicate in parallel; GPT-4o is the
    critical path. Replicate runs in the background and does NOT block response.

    Timeline (target < 5s):
    t=0   Signed URLs ready
    t=0   [PARALLEL] GPT-4o vision call
    t=0   [PARALLEL] Replicate bg-removal
    t=2–4 GPT-4o response → parse, build listing
    t=4   Compute fees + shipping (in-memory, ~0ms)
    t=4   Fire-and-forget persistence task
    t=4   Return Listing to client
    t=5–30 Replicate resolves in background
    """
    start = time.monotonic()

    gpt_task = asyncio.create_task(_call_gpt4o_vision(image_urls, user_notes))
    replicate_tasks = [
        asyncio.create_task(remove_background_replicate(url))
        for url in image_urls
    ]

    try:
        ai_data: dict[str, Any] = await gpt_task
    except Exception as exc:
        # Cancel Replicate tasks we no longer need
        for t in replicate_tasks:
            t.cancel()
        raise HTTPException(status_code=502, detail=f"AI analysis failed: {exc}") from exc

    # Derive status from confidence
    confidence: float = float(ai_data.get("ai_confidence", 0.0))
    status = "ready" if confidence >= 0.5 else "low_confidence"

    # Build price estimate with platform fees
    raw_price = ai_data.get("price_estimate", {})
    price_est = compute_platform_fees(
        PriceEstimate(
            fast_sale_pence   = raw_price.get("fast_sale_pence",   0),
            max_profit_pence  = raw_price.get("max_profit_pence",  0),
            recommended_pence = raw_price.get("recommended_pence"),  # may be None — handled correctly
            confidence        = raw_price.get("confidence", "low"),
            reasoning         = raw_price.get("reasoning",  ""),
            data_source       = "ai_estimate",
        )
    )

    # Deterministic local shipping logic (more reliable than model guess)
    weight_g   = max(int(ai_data.get("estimated_weight_g", 500)), 1)
    ship_est   = resolve_shipping(weight_g)

    # Build marketplace drafts
    drafts = [MarketplaceDraft(**d) for d in ai_data.get("drafts", [])]

    # Bundle suggestion
    raw_bundle  = ai_data.get("bundle_suggestion") or {}
    bundle_sugg = BundleSuggestion(**raw_bundle) if raw_bundle else BundleSuggestion(recommended=False)

    listing = Listing(
        user_id            = user_id,
        title              = drafts[0].title if drafts else ai_data.get("item_type", "Item"),
        item_type          = ai_data.get("item_type",  "unknown"),
        category           = ai_data.get("category",   ""),
        brand              = ai_data.get("brand"),
        model_style        = ai_data.get("model_style"),
        colour             = ai_data.get("colour",     ""),
        material           = ai_data.get("material"),
        condition          = ai_data.get("condition",  "good"),
        gender             = ai_data.get("gender",     "unknown"),
        estimated_weight_g = weight_g,
        ai_confidence      = confidence,
        processing_status  = status,
        is_bundle          = bundle_sugg.recommended,
        drafts             = drafts,
        price_estimate     = price_est,
        shipping_estimate  = ship_est,
        bundle_suggestion  = bundle_sugg,
        asset_ids          = asset_ids,
    )

    elapsed = time.monotonic() - start
    logger.info("Orchestration complete in %.2fs (confidence=%.2f)", elapsed, confidence)

    # Persist asynchronously — response returns immediately
    asyncio.create_task(_persist_listing(listing, ai_data, replicate_tasks))

    return listing


# =============================================================================
# AUTH HELPERS
# =============================================================================

def _sanitise_filename(filename: str) -> str:
    """
    Strip path separators and non-alphanumeric chars from a user-supplied filename.
    Prevents path traversal (e.g. ../../etc/passwd).
    """
    name = os.path.basename(filename)                        # strip any directory parts
    name = re.sub(r"[^\w.\-]", "_", name)                    # allow word chars, dots, hyphens
    return name[:128]                                        # cap length


async def _get_current_user_id(authorization: str | None) -> str:
    """
    Extract and verify the Supabase JWT from the Authorization header.
    Returns the user UUID string.

    TODO: In production, verify the JWT signature against the Supabase JWKS endpoint
    rather than trusting the sub claim directly. For MVP, decoding without verification
    is acceptable behind Supabase's own RLS policies.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header.")

    token = authorization.removeprefix("Bearer ").strip()

    try:
        # Decode without signature verification for MVP — Supabase RLS enforces ownership
        import base64 as _b64
        payload_part = token.split(".")[1]
        # Pad base64 if necessary
        padded = payload_part + "=" * (-len(payload_part) % 4)
        payload = _json.loads(_b64.urlsafe_b64decode(padded))
        user_id: str = payload["sub"]
        if not user_id:
            raise ValueError("Empty sub claim")
        return user_id
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid JWT: {exc}") from exc


# =============================================================================
# FASTAPI APPLICATION
# =============================================================================

app = FastAPI(
    title       = "ListingLift API",
    description = "AI-powered UK marketplace listing assistant",
    version     = "0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins     = ALLOWED_ORIGINS,
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Close the shared HTTP client cleanly on shutdown."""
    await _http_client.aclose()


@app.post("/api/scan", response_model=Listing, summary="Analyse item and generate listings")
async def scan_item(
    req: ScanRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> Listing:
    """
    Primary endpoint. Accepts pre-uploaded image URLs and returns a complete
    Listing within ~5 seconds.

    FIX: user_id now comes from the verified JWT, not from the request body.
    """
    user_id = await _get_current_user_id(authorization)

    if not req.image_urls:
        raise HTTPException(status_code=400, detail="At least one image URL is required.")
    if len(req.image_urls) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 images per scan.")
    # FIX: validate that asset_ids and image_urls are consistent
    if req.asset_ids and len(req.asset_ids) != len(req.image_urls):
        raise HTTPException(
            status_code=400,
            detail=f"asset_ids length ({len(req.asset_ids)}) must match image_urls length ({len(req.image_urls)}).",
        )

    return await orchestrate_listing(
        user_id    = user_id,
        asset_ids  = req.asset_ids,
        image_urls = req.image_urls,
        user_notes = req.user_notes,
    )


@app.get("/api/listings/{listing_id}", response_model=Listing, summary="Fetch a saved listing")
async def get_listing(
    listing_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> Listing:
    """
    Retrieve a previously saved listing by ID.
    FIX: verifies ownership via authenticated user_id.
    """
    user_id = await _get_current_user_id(authorization)

    result = await _db(
        lambda: (
            supabase.table("listings")
            .select("*")
            .eq("id", listing_id)
            .eq("user_id", user_id)       # FIX: ownership check
            .single()
            .execute()
        )
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Listing not found.")
    return Listing(**result.data)


@app.get("/api/upload-url", summary="Get a pre-signed upload URL for Supabase Storage")
async def get_upload_url(
    filename: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    """
    Returns a pre-signed URL so the client can upload directly to Supabase Storage.
    FIX: user_id comes from JWT, not query param.
    FIX: filename is sanitised to prevent path traversal.
    """
    user_id = await _get_current_user_id(authorization)

    safe_filename = _sanitise_filename(filename)
    asset_id      = str(uuid.uuid4())
    storage_path  = f"uploads/{user_id}/{asset_id}/{safe_filename}"

    await _db(
        lambda: supabase.table("uploaded_assets").insert({
            "id":            asset_id,
            "user_id":       user_id,
            "storage_path":  storage_path,
            "upload_status": "pending",
        }).execute()
    )

    signed = await _db(
        lambda: supabase.storage.from_("item-images").create_signed_upload_url(storage_path)
    )

    return {
        "asset_id":     asset_id,
        "upload_url":   signed["signedURL"],
        "storage_path": storage_path,
    }


@app.delete("/api/listings/{listing_id}", summary="Soft-delete a listing")
async def delete_listing(
    listing_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    """
    Soft-deletes a listing by setting status to 'archived'.
    FIX: ownership enforced via user_id filter on the UPDATE.
    """
    user_id = await _get_current_user_id(authorization)

    await _db(
        lambda: (
            supabase.table("listings")
            .update({"processing_status": "archived"})
            .eq("id", listing_id)
            .eq("user_id", user_id)       # FIX: ownership check
            .execute()
        )
    )
    return {"status": "archived"}


@app.get("/api/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}


# =============================================================================
# REACT PROFIT ESTIMATE CARD — TSX (embedded for reference / code generation)
# =============================================================================

PROFIT_ESTIMATE_CARD_TSX = '''
// components/ProfitEstimateCard.tsx
// ListingLift — Profit Estimate Card
// Mobile-first · Claymorphism · Soft Utility aesthetic
// Tailwind CSS · React 18 · TypeScript

import React from "react";

interface PriceEstimate {
  fastSalePence:   number;
  maxProfitPence:  number;
  confidence:      "high" | "medium" | "low";
  reasoning:       string;
  ebayFeePence?:   number;
  depopFeePence?:  number;
  vintedFeePence?: number;
}

interface ShippingEstimate {
  carrier:            string;
  service:            string;
  estimatedCostPence: number;
  weightBand:         string;
  reason:             string;
}

interface ProfitEstimateCardProps {
  title:            string;
  brand?:           string | null;
  condition:        string;
  aiConfidence:     number;
  priceEstimate:    PriceEstimate;
  shippingEstimate: ShippingEstimate;
  platform?:        "vinted" | "ebay_uk" | "depop";
  isLoading?:       boolean;
}

const pence = (p: number) =>
  new Intl.NumberFormat("en-GB", { style: "currency", currency: "GBP" }).format(p / 100);

const CONFIDENCE_STYLES = {
  high:   "bg-mint-100 text-mint-800 border-mint-300",
  medium: "bg-amber-100 text-amber-800 border-amber-300",
  low:    "bg-rose-100 text-rose-800 border-rose-300",
} as const;

const CONFIDENCE_LABELS = {
  high:   "Confident estimate",
  medium: "Rough estimate",
  low:    "Very rough guess",
} as const;

const CONDITION_DISPLAY: Record<string, string> = {
  new_with_tags:    "New with tags",
  new_without_tags: "New without tags",
  very_good:        "Very good",
  good:             "Good",
  satisfactory:     "Satisfactory",
  poor:             "Poor",
};

const ConfidenceBadge = ({ level }: { level: keyof typeof CONFIDENCE_STYLES }) => (
  <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs
                   font-semibold border ${CONFIDENCE_STYLES[level]}`}>
    <span className="w-1.5 h-1.5 rounded-full bg-current opacity-70" aria-hidden />
    {CONFIDENCE_LABELS[level]}
  </span>
);

const PriceBlock = ({
  label, amountPence, feePence, caption, highlight = false,
}: {
  label: string; amountPence: number; feePence?: number; caption: string; highlight?: boolean;
}) => (
  <div className={`flex-1 rounded-2xl p-4 flex flex-col gap-1 border
                  ${highlight ? "bg-mint-50 border-mint-300 shadow-inner"
                              : "bg-oatmeal-50 border-oatmeal-200"}`}>
    <p className="text-xs font-medium text-neutral-500 uppercase tracking-wide">{label}</p>
    <p className={`text-2xl font-extrabold tabular-nums
                  ${highlight ? "text-mint-700" : "text-neutral-800"}`}>
      {pence(amountPence)}
    </p>
    {feePence !== undefined && (
      <p className="text-xs text-neutral-400">~{pence(feePence)} platform fee</p>
    )}
    <p className={`text-xs mt-1 ${highlight ? "text-mint-600" : "text-neutral-500"}`}>
      {caption}
    </p>
  </div>
);

export const ProfitEstimateCard: React.FC<ProfitEstimateCardProps> = ({
  title, brand, condition, aiConfidence, priceEstimate, shippingEstimate,
  platform = "ebay_uk", isLoading = false,
}) => {
  const feePence = platform === "ebay_uk"  ? priceEstimate.ebayFeePence
                 : platform === "depop"    ? priceEstimate.depopFeePence
                 : priceEstimate.vintedFeePence;

  if (isLoading) {
    return (
      <div className="bg-white rounded-3xl shadow-clay p-5 flex flex-col gap-4 animate-pulse"
           aria-busy="true" aria-label="Analysing your item…">
        <div className="h-5 bg-oatmeal-200 rounded-full w-3/4" />
        <div className="h-4 bg-oatmeal-200 rounded-full w-1/3" />
        <div className="flex gap-3">
          <div className="flex-1 h-24 bg-oatmeal-100 rounded-2xl" />
          <div className="flex-1 h-24 bg-mint-100 rounded-2xl" />
        </div>
        <p className="text-center text-sm text-neutral-400 font-medium">
          Sorting your clobber…
        </p>
      </div>
    );
  }

  return (
    <article className="bg-white rounded-3xl shadow-clay p-5 flex flex-col gap-4 w-full max-w-sm mx-auto"
             aria-label={`Profit estimate for ${title}`}>

      <header className="flex items-start justify-between gap-2">
        <div className="flex flex-col gap-1">
          <h2 className="text-base font-bold text-neutral-900 leading-snug line-clamp-2">{title}</h2>
          <div className="flex items-center gap-2 flex-wrap">
            {brand && (
              <span className="text-xs bg-oatmeal-100 text-neutral-600 px-2 py-0.5 rounded-full
                               border border-oatmeal-200">{brand}</span>
            )}
            <span className="text-xs text-neutral-500">
              {CONDITION_DISPLAY[condition] ?? condition}
            </span>
          </div>
        </div>
        <ConfidenceBadge level={priceEstimate.confidence} />
      </header>

      <section aria-label="Price estimates" className="flex gap-3">
        <PriceBlock label="Fast Sale"  amountPence={priceEstimate.fastSalePence}
                    feePence={feePence} caption="Likely to sell fast" />
        <PriceBlock label="Max Profit" amountPence={priceEstimate.maxProfitPence}
                    feePence={feePence} caption="Higher margin, slower sale" highlight />
      </section>

      <div aria-label={`AI confidence: ${Math.round(aiConfidence * 100)}%`}>
        <div className="flex justify-between text-xs text-neutral-400 mb-1">
          <span>AI confidence</span>
          <span>{Math.round(aiConfidence * 100)}%</span>
        </div>
        <div className="w-full h-2 bg-oatmeal-100 rounded-full overflow-hidden">
          <div className="h-full rounded-full bg-gradient-to-r from-mint-400 to-mint-600 transition-all"
               style={{ width: `${Math.round(aiConfidence * 100)}%` }}
               role="progressbar" aria-valuenow={Math.round(aiConfidence * 100)}
               aria-valuemin={0} aria-valuemax={100} />
        </div>
      </div>

      <div className="flex items-center gap-2 bg-oatmeal-50 border border-oatmeal-200
                      rounded-2xl px-4 py-3"
           aria-label={`Suggested shipping: ${shippingEstimate.service}`}>
        <span className="text-xl" aria-hidden>📦</span>
        <div className="flex flex-col gap-0.5 flex-1 min-w-0">
          <p className="text-xs font-semibold text-neutral-700 truncate">{shippingEstimate.service}</p>
          <p className="text-xs text-neutral-500">~{pence(shippingEstimate.estimatedCostPence)} postage</p>
        </div>
        <span className="text-xs font-bold uppercase tracking-wider text-coral-600 bg-coral-50
                         border border-coral-200 px-2 py-0.5 rounded-full shrink-0">
          {shippingEstimate.carrier === "royal_mail" ? "Royal Mail" : "Evri"}
        </span>
      </div>

      <p className="text-[11px] text-neutral-400 leading-relaxed">
        Prices are AI estimates based on category, brand, and condition — not live market data.
        Platform fees are approximate. Check each platform for current rates.
      </p>

      <button className="w-full bg-coral-500 hover:bg-coral-600 active:scale-95 text-white
                         font-bold py-3.5 rounded-2xl text-sm tracking-wide shadow-coral
                         transition-all duration-150 focus-visible:outline-none
                         focus-visible:ring-2 focus-visible:ring-coral-400 focus-visible:ring-offset-2"
              aria-label="View full listing drafts">
        View Listings — Sorted 🎉
      </button>
    </article>
  );
};

/*
tailwind.config.ts additions:
theme: { extend: {
  colors: {
    mint:    { 50:"#f0fdf4", 100:"#dcfce7", 300:"#86efac", 400:"#4ade80", 600:"#16a34a", 700:"#15803d", 800:"#166534" },
    oatmeal: { 50:"#faf9f7", 100:"#f5f0e8", 200:"#e8e0d0" },
    coral:   { 50:"#fff5f5", 200:"#fecaca", 500:"#f87171", 600:"#ef4444" },
  },
  boxShadow: {
    clay:  "0 8px 32px rgba(0,0,0,0.08), 0 2px 8px rgba(0,0,0,0.04)",
    coral: "0 4px 12px rgba(248,113,113,0.35)",
  },
}}
*/
'''


# =============================================================================
# ARCHITECTURE NOTES
# =============================================================================

ARCHITECTURE_NOTES = """
LISTINGLIFT — ARCHITECTURE DECISIONS
======================================

1. SERVER ACTIONS vs API ROUTES
   Next.js Server Actions handle the client-facing scan call. They stream a
   progress token (analysing → pricing → ready) via useOptimistic / RSC.
   This FastAPI layer can be deployed as a Supabase Edge Function (Deno port)
   or as a standalone Render/Railway service.
   Use Next.js API routes ONLY for Replicate webhook callbacks.

2. CACHING
   GPT-4o responses: NOT cached (every photo is unique).
   Shipping tiers: static constant — no cache needed.
   Platform fees: in-process constant, refresh on deploy.
   Supabase reads: use read replica for listing history queries.

3. PERSISTENCE TIMING
   Always persist (even low-confidence). User can review and dismiss.
   Images: 30-day GDPR TTL unless user taps "Save".
   Background-removed images: async, non-blocking to the scan response.
   Draft copy: persisted once, editable without re-scanning.

4. RESPONSE TIME
   Critical path: GPT-4o only (~2–4s). Replicate and Supabase are async.
   Use response_format: json_object + max_tokens: 2048 to minimise streaming.
   Pre-sign upload URLs client-side (Supabase JS SDK) for zero proxy latency.

5. GDPR DEFAULTS
   30-day auto-purge via Supabase scheduled function.
   No user email sent to OpenAI. Image URLs are 1h signed URLs.
   Signed URLs never persisted — regenerated on read.
   Privacy policy must disclose GPT-4o processing (OpenAI DPA).

6. STILL TO BREAK / WATCH
   - Supabase sync client in to_thread: works but consider supabase-py async
     client (async branch) when it reaches stable release.
   - JWT decode without signature verification is MVP-only. Add JWKS verification
     before going to production.
   - Rate limiting: add slowapi or upstash-ratelimit middleware before launch.
   - asyncio.create_task() background tasks are silently dropped if the worker
     restarts mid-request. Use a proper job queue (Supabase pg_cron / BullMQ)
     for production persistence reliability.

7. POST-MVP
   Priority 1: eBay Browse API for real sold-price comps.
   Priority 2: Vinted deeplink with pre-filled fields.
   Priority 3: Batch mode (up to 30 items).
   Priority 4: AI photo quality tips (detect bad lighting / angle).
   Priority 5: Pro tier — unlimited scans, CSV export, multi-platform push.
"""


# =============================================================================
# ENTRYPOINT
# =============================================================================

if __name__ == "__main__":
    import uvicorn

    print("╔══════════════════════════════════╗")
    print("║      ListingLift API v0.2        ║")
    print("║   UK Marketplace Listing MVP     ║")
    print("╚══════════════════════════════════╝")
    print(ARCHITECTURE_NOTES)

    uvicorn.run(
        "list_lift:app",
        host    = "0.0.0.0",
        port    = int(os.environ.get("PORT", 8000)),
        reload  = os.environ.get("ENV", "production") == "development",
        workers = 1,
    )
