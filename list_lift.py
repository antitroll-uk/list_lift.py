"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                          L I S T I N G L I F T                             ║
║           AI-Powered UK Marketplace Listing Assistant — MVP                 ║
║                                                                              ║
║  Stack: FastAPI · Supabase · OpenAI GPT-4o · Replicate · Tailwind/Next.js  ║
║  Author: Generated via ListingLift spec · Date: 2026-04-13                  ║
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
3.  GPT-4o vision receives a public or signed URL; base64 is used only as
    fallback if the URL is inaccessible.
4.  Pricing guidance is AI-estimated — no live market feed at MVP. Clearly
    disclosed to users.
5.  Replicate background-removal runs in parallel with GPT-4o analysis;
    results are merged before the final response.
6.  All listings are soft-deleted; hard deletion runs via a nightly GDPR job
    that purges assets older than 30 days unless the user saves them.
7.  Platform fees shown are approximate (eBay ~12.8%, Depop ~10%, Vinted ~5%)
    and are disclosed as estimates.
8.  Supabase Edge Functions handle the heavy async orchestration;
    Next.js Server Actions call those functions and stream progress events.
9.  Rate limiting is applied at the Supabase edge (30 scans/user/day free tier).
10. All monetary values stored and returned in pence (GBP) to avoid float
    precision issues; displayed in pounds (£) in the UI.

SUPABASE DATABASE SCHEMA (PostgreSQL DDL)
-----------------------------------------
-- Run this in the Supabase SQL editor to bootstrap the MVP schema.

/*
--------------------------------------------------------------------
TABLE: users  (extends Supabase auth.users)
Purpose: Public-facing profile and tier information.
--------------------------------------------------------------------
*/
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
-- Index on tier for quota enforcement queries
CREATE INDEX IF NOT EXISTS idx_users_tier ON public.users(tier);

/*
--------------------------------------------------------------------
TABLE: uploaded_assets
Purpose: Tracks each uploaded image file. NOT a permanent media store —
         assets are deleted after 30 days unless listing is saved.
--------------------------------------------------------------------
*/
CREATE TABLE IF NOT EXISTS public.uploaded_assets (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID        NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    listing_id      UUID,                          -- FK added after listing created
    storage_path    TEXT        NOT NULL,           -- Supabase Storage path
    signed_url      TEXT,                          -- ephemeral; regenerated on read
    bg_removed_path TEXT,                          -- Replicate output path
    mime_type       TEXT        NOT NULL DEFAULT 'image/jpeg',
    size_bytes      INTEGER,
    width_px        INTEGER,
    height_px       INTEGER,
    upload_status   TEXT        NOT NULL DEFAULT 'pending'
                                CHECK (upload_status IN ('pending','uploaded','processing','ready','failed')),
    gdpr_purge_at   TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '30 days'),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_assets_user_id    ON public.uploaded_assets(user_id);
CREATE INDEX IF NOT EXISTS idx_assets_listing_id ON public.uploaded_assets(listing_id);
CREATE INDEX IF NOT EXISTS idx_assets_purge_at   ON public.uploaded_assets(gdpr_purge_at);
-- PRIVACY NOTE: storage_path and signed_url must NEVER be exposed to other users.

/*
--------------------------------------------------------------------
TABLE: listings
Purpose: Core listing record. Anchors all related drafts and estimates.
--------------------------------------------------------------------
*/
CREATE TABLE IF NOT EXISTS public.listings (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID        NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    title               TEXT,                          -- AI-generated working title
    item_type           TEXT,                          -- e.g. 'jacket', 'trainers'
    category            TEXT,                          -- e.g. 'Menswear > Footwear'
    brand               TEXT,
    model_style         TEXT,
    colour              TEXT,
    material            TEXT,
    condition           TEXT        CHECK (condition IN (
                                        'new_with_tags','new_without_tags',
                                        'very_good','good','satisfactory','poor')),
    gender              TEXT        CHECK (gender IN ('mens','womens','unisex','boys','girls','unknown')),
    estimated_weight_g  INTEGER,                       -- conservative estimate in grams
    ai_confidence       NUMERIC(4,3) CHECK (ai_confidence BETWEEN 0 AND 1),
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

/*
--------------------------------------------------------------------
TABLE: marketplace_drafts
Purpose: One row per platform per listing. Stores AI-generated copy.
--------------------------------------------------------------------
*/
CREATE TABLE IF NOT EXISTS public.marketplace_drafts (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    listing_id      UUID        NOT NULL REFERENCES public.listings(id) ON DELETE CASCADE,
    platform        TEXT        NOT NULL CHECK (platform IN ('vinted','ebay_uk','depop')),
    title           TEXT,
    description     TEXT,
    hashtags        TEXT[],                -- Depop only; max 5
    category_path   TEXT,                  -- platform-native category string
    condition_label TEXT,                  -- platform-native condition string
    keywords        TEXT[],                -- eBay UK: SEO keywords used in title
    raw_json        JSONB,                 -- full AI response for this platform
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(listing_id, platform)
);
CREATE INDEX IF NOT EXISTS idx_drafts_listing_id ON public.marketplace_drafts(listing_id);

/*
--------------------------------------------------------------------
TABLE: price_estimates
Purpose: AI-generated pricing guidance. Stored in pence.
--------------------------------------------------------------------
*/
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

/*
--------------------------------------------------------------------
TABLE: shipping_estimates
Purpose: UK shipping recommendation per listing.
--------------------------------------------------------------------
*/
CREATE TABLE IF NOT EXISTS public.shipping_estimates (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    listing_id          UUID        NOT NULL UNIQUE REFERENCES public.listings(id) ON DELETE CASCADE,
    weight_band         TEXT        NOT NULL CHECK (weight_band IN (
                                        'small_parcel_0_100g',
                                        'small_parcel_101_250g',
                                        'small_parcel_251_500g',
                                        'small_parcel_501_750g',
                                        'small_parcel_751_1000g',
                                        'medium_parcel_1_2kg',
                                        'large_parcel_2_5kg',
                                        'large_parcel_5kg_plus')),
    carrier             TEXT        NOT NULL CHECK (carrier IN ('royal_mail','evri','hermes')),
    service             TEXT,                          -- e.g. 'Royal Mail Tracked 48'
    estimated_cost_pence INTEGER,
    reason              TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

/*
--------------------------------------------------------------------
TABLE: bundle_suggestions
Purpose: If multiple assets look like related items, suggest a bundle.
--------------------------------------------------------------------
*/
CREATE TABLE IF NOT EXISTS public.bundle_suggestions (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    listing_id          UUID        NOT NULL REFERENCES public.listings(id) ON DELETE CASCADE,
    recommended         BOOLEAN     NOT NULL DEFAULT FALSE,
    asset_ids           UUID[],                        -- which uploaded_assets to bundle
    bundle_title        TEXT,
    bundle_reason       TEXT,
    suggested_price_pence INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Row-level security: users may only read/write their own data
ALTER TABLE public.users               ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.uploaded_assets     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.listings            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.marketplace_drafts  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.price_estimates     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.shipping_estimates  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.bundle_suggestions  ENABLE ROW LEVEL SECURITY;

-- Example RLS policy (repeat pattern for all tables)
CREATE POLICY user_owns_listing ON public.listings
    FOR ALL USING (auth.uid() = user_id);

"""

# =============================================================================
# PYTHON DEPENDENCIES
# pip install fastapi uvicorn supabase openai httpx python-dotenv pydantic
# =============================================================================

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from enum import Enum
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from supabase import create_client, Client

load_dotenv()

# =============================================================================
# CONFIGURATION
# =============================================================================

SUPABASE_URL: str = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY: str = os.environ["SUPABASE_SERVICE_KEY"]
OPENAI_API_KEY: str = os.environ["OPENAI_API_KEY"]
REPLICATE_API_TOKEN: str = os.environ.get("REPLICATE_API_TOKEN", "")
ALLOWED_ORIGINS: list[str] = os.environ.get(
    "ALLOWED_ORIGINS", "http://localhost:3000"
).split(",")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Platform fee rates (approximate, disclosed as estimates)
PLATFORM_FEES = {
    "ebay_uk": 0.128,   # ~12.8% including PayPal
    "depop":   0.100,   # 10%
    "vinted":  0.050,   # ~5% buyer protection fee (seller usually pays nothing)
}

# UK shipping tiers — weight in grams → (carrier, service, cost_pence)
SHIPPING_TIERS = [
    (100,  "royal_mail", "Royal Mail Large Letter",         "small_parcel_0_100g",      135),
    (250,  "royal_mail", "Royal Mail Tracked 48 (0–250g)",  "small_parcel_101_250g",    285),
    (500,  "royal_mail", "Royal Mail Tracked 48 (250–500g)","small_parcel_251_500g",    330),
    (750,  "royal_mail", "Royal Mail Tracked 48 (500–750g)","small_parcel_501_750g",    380),
    (1000, "royal_mail", "Royal Mail Tracked 48 (<1kg)",    "small_parcel_751_1000g",   430),
    (2000, "evri",       "Evri Parcel (1–2kg)",             "medium_parcel_1_2kg",      299),
    (5000, "evri",       "Evri Parcel (2–5kg)",             "large_parcel_2_5kg",       399),
    (None, "evri",       "Evri Large Parcel (5kg+)",        "large_parcel_5kg_plus",    599),
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
    hashtags:        list[str] = Field(default_factory=list, max_length=5)
    category_path:   str | None = None
    condition_label: str | None = None
    keywords:        list[str] = Field(default_factory=list)


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
    weight_band:           str
    carrier:               str
    service:               str
    estimated_cost_pence:  int
    reason:                str


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
    id:                  str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id:             str
    title:               str
    item_type:           str
    category:            str
    brand:               str | None = None
    model_style:         str | None = None
    colour:              str
    material:            str | None = None
    condition:           ConditionEnum
    gender:              GenderEnum = GenderEnum.unknown
    estimated_weight_g:  int
    ai_confidence:       float = Field(ge=0, le=1)
    processing_status:   str = "ready"
    is_bundle:           bool = False
    drafts:              list[MarketplaceDraft] = Field(default_factory=list)
    price_estimate:      PriceEstimate | None = None
    shipping_estimate:   ShippingEstimate | None = None
    bundle_suggestion:   BundleSuggestion | None = None
    asset_ids:           list[str] = Field(default_factory=list)
    created_at:          str | None = None


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
- item_type        : the specific type (e.g. "hooded sweatshirt", "Chelsea boots")
- category         : hierarchical path (e.g. "Menswear > Tops > Hoodies & Sweatshirts")
- brand            : only if a logo, label, or stitching is clearly visible — otherwise null
- model_style      : style or model name if identifiable — otherwise null
- colour           : primary colour in British English (e.g. "navy blue", "oatmeal")
- material         : inferred material (e.g. "cotton jersey", "full-grain leather")
- condition        : one of: new_with_tags | new_without_tags | very_good | good |
                     satisfactory | poor
- condition_notes  : brief honest note on visible flaws or wear (e.g. "slight pilling on
                     cuffs, no holes")
- gender           : mens | womens | unisex | boys | girls | unknown
- estimated_weight_g : conservative integer estimate in grams (include garment bag/box
                       if visible; round up if uncertain)
- ai_confidence    : float 0–1 reflecting your certainty across all fields combined

CONFIDENCE RULES
- Set ai_confidence >= 0.80 only if brand/type/condition are all clearly identifiable.
- Set ai_confidence 0.50–0.79 if at least type and condition are clear but brand is uncertain.
- Set ai_confidence < 0.50 if the image is blurry, item is obscured, or type is ambiguous.
- When uncertain about brand or model, set those fields to null. Never guess brand names.
- If image quality is too low to reliably identify the item, set processing_status to
  "low_confidence" and explain in condition_notes.

LISTING DRAFTS
Generate three marketplace-specific listing drafts. Each must feel native to its platform.

--- VINTED DRAFT ---
Platform voice: clean, honest, friendly. Buyers on Vinted want quick facts and fair prices.
- title: 40–60 chars, plain, descriptive (e.g. "Nike Air Max 90 Trainers – Size 9 – Good Cond")
- description: 3–5 short sentences. Lead with condition. Mention key details. End with
  postage note. No hashtags. No hyperbole.
- category_path: Vinted-style path (e.g. "Women > Shoes > Trainers & Sporty Shoes")
- condition_label: Vinted label (New with tags | New without tags | Very good | Good |
  Satisfactory)

--- EBAY UK DRAFT ---
Platform voice: search-optimised, trustworthy, detailed.
- title: HARD LIMIT of 80 characters. Front-load brand + type + key attributes.
  Use title case. No punctuation other than hyphens. No padding words like "Look" or "Rare".
  Example: "Nike Air Max 90 White Trainers UK 9 Mens Leather Running Shoes VGC"
- description: 60–120 words. Bullet the key specs. Include honest condition note.
  Mention "Tracked postage" and "returns accepted". No HTML tags.
- keywords: list of 5–8 SEO terms used in or relevant to the title
- category_path: eBay UK category path (e.g. "Clothes, Shoes & Accessories > Men's Shoes")
- condition_label: eBay label (New with tags | New without box | Very Good | Good | Acceptable)

--- DEPOP DRAFT ---
Platform voice: fashion-forward, visual, youth-aware but not forced.
- title: 30–50 chars. Style-aware, trend-aware where genuine. Use lower case.
  Example: "vintage levi's 501 jeans w30 l32 – 90s wash"
- description: 3–6 short punchy lines. Lead with vibe, follow with facts.
  Mention measurements if clothing. End with shipping note.
- hashtags: EXACTLY 5 relevant hashtags (include # prefix, all lowercase, no spaces)
  e.g. ["#vintage", "#levis", "#denimjeans", "#90sfashion", "#slowfashion"]
- category_path: Depop-style path (e.g. "Menswear > Bottoms > Jeans")

PRICING GUIDANCE
Provide conservative pricing in pence (GBP).
- fast_sale_pence:   price likely to sell within 48 hours
- max_profit_pence:  price that maximises margin if seller is patient
- confidence:        high | medium | low
- reasoning:         one sentence explaining basis (brand, condition, category, market norms)
- IMPORTANT: You have no access to live market data. State this clearly in reasoning.
  Base estimates on your training knowledge of typical UK second-hand market values.

SHIPPING LOGIC
Recommend UK shipping based on estimated_weight_g:
- 0–100g:    Royal Mail Large Letter (~£1.35)
- 101–500g:  Royal Mail Tracked 48 (~£2.85–£3.30)
- 501–1000g: Royal Mail Tracked 48 (~£3.80–£4.30)
- 1–2kg:     Evri Parcel (~£2.99)
- 2–5kg:     Evri Parcel (~£3.99)
- 5kg+:      Evri Large Parcel (~£5.99)
Prefer Royal Mail for items under 1kg; Evri for heavier parcels.
Include reason field explaining the recommendation.

BUNDLE DETECTION
If multiple images appear to show DIFFERENT items (not multiple angles of one item):
- Set bundle_recommended to true
- Suggest which items go together and why
- Estimate a combined bundle price in pence

OUTPUT FORMAT
Return ONLY valid JSON matching the schema below. No prose before or after.
If you cannot identify the item reliably, still return the JSON with
ai_confidence < 0.5 and processing_status = "low_confidence".
"""

# =============================================================================
# JSON SCHEMA FOR GPT-4o OUTPUT
# =============================================================================

GPT4O_OUTPUT_SCHEMA = {
    "type": "object",
    "required": [
        "processing_status", "item_type", "category", "colour",
        "condition", "gender", "estimated_weight_g", "ai_confidence",
        "drafts", "price_estimate", "shipping_estimate", "bundle_suggestion"
    ],
    "properties": {
        "processing_status": {
            "type": "string",
            "enum": ["ready", "low_confidence"]
        },
        "item_type":           {"type": "string"},
        "category":            {"type": "string"},
        "brand":               {"type": ["string", "null"]},
        "model_style":         {"type": ["string", "null"]},
        "colour":              {"type": "string"},
        "material":            {"type": ["string", "null"]},
        "condition": {
            "type": "string",
            "enum": ["new_with_tags","new_without_tags","very_good","good","satisfactory","poor"]
        },
        "condition_notes":     {"type": ["string", "null"]},
        "gender": {
            "type": "string",
            "enum": ["mens","womens","unisex","boys","girls","unknown"]
        },
        "estimated_weight_g":  {"type": "integer", "minimum": 1},
        "ai_confidence":       {"type": "number", "minimum": 0, "maximum": 1},
        "drafts": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "required": ["platform", "title", "description"],
                "properties": {
                    "platform":       {"type": "string", "enum": ["vinted","ebay_uk","depop"]},
                    "title":          {"type": "string"},
                    "description":    {"type": "string"},
                    "hashtags":       {"type": "array", "items": {"type": "string"}, "maxItems": 5},
                    "category_path":  {"type": ["string", "null"]},
                    "condition_label":{"type": ["string", "null"]},
                    "keywords":       {"type": "array", "items": {"type": "string"}}
                }
            }
        },
        "price_estimate": {
            "type": "object",
            "required": ["fast_sale_pence","max_profit_pence","confidence","reasoning"],
            "properties": {
                "fast_sale_pence":   {"type": "integer", "minimum": 0},
                "max_profit_pence":  {"type": "integer", "minimum": 0},
                "confidence":        {"type": "string", "enum": ["high","medium","low"]},
                "reasoning":         {"type": "string"}
            }
        },
        "shipping_estimate": {
            "type": "object",
            "required": ["weight_band","carrier","service","estimated_cost_pence","reason"],
            "properties": {
                "weight_band":          {"type": "string"},
                "carrier":              {"type": "string"},
                "service":              {"type": "string"},
                "estimated_cost_pence": {"type": "integer"},
                "reason":               {"type": "string"}
            }
        },
        "bundle_suggestion": {
            "type": "object",
            "required": ["recommended"],
            "properties": {
                "recommended":           {"type": "boolean"},
                "bundle_title":          {"type": ["string", "null"]},
                "bundle_reason":         {"type": ["string", "null"]},
                "suggested_price_pence": {"type": ["integer", "null"]}
            }
        }
    }
}

# =============================================================================
# EXAMPLE GPT-4o MODEL OUTPUT
# =============================================================================

EXAMPLE_GPT4O_OUTPUT = {
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
                "In very good condition — no stains or holes, just a slight crease "
                "on one cuff that'll wash out. Heavyweight cotton so it's proper warm. "
                "Happy to bundle with other items. Tracked postage included in price."
            ),
            "hashtags": [],
            "category_path": "Men > Clothing > Hoodies & Sweatshirts",
            "condition_label": "Very good",
            "keywords": []
        },
        {
            "platform": "ebay_uk",
            "title": "Champion Reverse Weave Hoodie Navy Blue Mens Size L Heavyweight VGC",
            "description": (
                "Champion Reverse Weave Hoodie\n"
                "• Colour: Navy Blue\n"
                "• Size: Large (fits true to size)\n"
                "• Material: Heavyweight cotton fleece\n"
                "• Condition: Very Good — light crease on one cuff, otherwise excellent\n"
                "• Genuine Champion item with embroidered logo\n\n"
                "Dispatched within 1 working day via Royal Mail Tracked 48. "
                "Returns accepted within 30 days. Please check measurements before buying."
            ),
            "hashtags": [],
            "category_path": "Clothes, Shoes & Accessories > Men's Clothing > Hoodies & Sweatshirts",
            "condition_label": "Very Good",
            "keywords": ["champion","reverse weave","hoodie","navy","heavyweight","cotton","streetwear"]
        },
        {
            "platform": "depop",
            "title": "champion reverse weave hoodie navy – size L",
            "description": (
                "the heavyweight OG. champion reverse weave in proper navy 🏆\n"
                "size L – fits relaxed, great oversized on M\n"
                "condition: very good – tiny crease on cuff, nothing major\n"
                "chest 56cm / length 71cm\n"
                "tracked postage £4.30 or collect in person"
            ),
            "hashtags": ["#champion","#reversweave","#hoodie","#streetwear","#vintageathleticwear"],
            "category_path": "Menswear > Tops > Hoodies",
            "condition_label": "Good",
            "keywords": []
        }
    ],
    "price_estimate": {
        "fast_sale_pence": 2200,
        "max_profit_pence": 3200,
        "confidence": "medium",
        "reasoning": (
            "Estimated from training data; no live market feed. Champion Reverse Weave "
            "in good/very good condition typically sells between £22–£35 on UK platforms. "
            "Navy colourways are popular. Fast-sale price set conservatively."
        )
    },
    "shipping_estimate": {
        "weight_band": "small_parcel_501_750g",
        "carrier": "royal_mail",
        "service": "Royal Mail Tracked 48 (500–750g)",
        "estimated_cost_pence": 380,
        "reason": (
            "Heavyweight hoodie estimated at ~680g. Royal Mail Tracked 48 is the most "
            "cost-effective tracked option under 1kg and familiar to UK buyers."
        )
    },
    "bundle_suggestion": {
        "recommended": False,
        "bundle_title": None,
        "bundle_reason": None,
        "suggested_price_pence": None
    }
}

# =============================================================================
# BACKEND WORKFLOW — CORE ORCHESTRATION
# =============================================================================

async def remove_background_replicate(image_url: str) -> str | None:
    """
    Calls Replicate's background removal model (BRIA RMBG 1.4) asynchronously.
    Returns the output image URL or None if unavailable / token not set.
    """
    if not REPLICATE_API_TOKEN:
        return None

    headers = {
        "Authorization": f"Token {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "version": "a3d2e394b36a4b2ef93a61d4b980b5c5c4e4f6e0",  # BRIA RMBG 1.4
        "input": {"image": image_url}
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.replicate.com/v1/predictions",
            headers=headers,
            json=payload,
        )
        if resp.status_code != 201:
            return None
        prediction = resp.json()
        prediction_id = prediction["id"]

        # Poll for completion (max 25s for MVP)
        for _ in range(25):
            await asyncio.sleep(1)
            poll = await client.get(
                f"https://api.replicate.com/v1/predictions/{prediction_id}",
                headers=headers,
            )
            result = poll.json()
            if result["status"] == "succeeded":
                return result["output"]
            if result["status"] in ("failed", "canceled"):
                return None
    return None


async def call_gpt4o_vision(image_urls: list[str], user_prompt: str = "") -> dict[str, Any]:
    """
    Sends one or more image URLs to GPT-4o with the ListingLift system prompt.
    Returns parsed JSON or raises an exception.

    Parallel strategy: background removal and GPT-4o are kicked off concurrently
    via asyncio.gather() in the main orchestrator.
    """
    content: list[dict] = []

    # Add each image
    for url in image_urls:
        content.append({
            "type": "image_url",
            "image_url": {"url": url, "detail": "high"}
        })

    # Append user context if provided
    if user_prompt:
        content.append({"type": "text", "text": user_prompt})
    else:
        content.append({
            "type": "text",
            "text": (
                "Please analyse this item thoroughly and return the full JSON "
                "listing data as specified."
            )
        })

    response = await openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": GPT4O_SYSTEM_PROMPT},
            {"role": "user",   "content": content},
        ],
        response_format={"type": "json_object"},
        max_tokens=2048,
        temperature=0.3,   # low temp for structured, consistent outputs
    )

    raw = response.choices[0].message.content
    return json.loads(raw)


def compute_platform_fees(price_estimate: PriceEstimate) -> PriceEstimate:
    """
    Augments a PriceEstimate with per-platform fee calculations.
    Uses the recommended_pence (or midpoint) as the base price.
    All values in pence.
    """
    base = price_estimate.recommended_pence or (
        (price_estimate.fast_sale_pence + price_estimate.max_profit_pence) // 2
    )
    price_estimate.ebay_fee_pence   = int(base * PLATFORM_FEES["ebay_uk"])
    price_estimate.depop_fee_pence  = int(base * PLATFORM_FEES["depop"])
    price_estimate.vinted_fee_pence = int(base * PLATFORM_FEES["vinted"])
    return price_estimate


def resolve_shipping(weight_g: int) -> dict[str, Any]:
    """
    Deterministically resolve the best UK shipping option from the weight tiers.
    Returns a dict compatible with ShippingEstimate.
    """
    for (max_g, carrier, service, weight_band, cost_pence) in SHIPPING_TIERS:
        if max_g is None or weight_g <= max_g:
            return {
                "weight_band": weight_band,
                "carrier": carrier,
                "service": service,
                "estimated_cost_pence": cost_pence,
                "reason": (
                    f"Item estimated at ~{weight_g}g. "
                    f"{service} is the most cost-effective tracked option for this weight."
                )
            }
    # Fallback (should not reach here)
    return {
        "weight_band": "large_parcel_5kg_plus",
        "carrier": "evri",
        "service": "Evri Large Parcel",
        "estimated_cost_pence": 599,
        "reason": "Item appears heavy; Evri Large Parcel recommended."
    }


async def orchestrate_listing(
    user_id: str,
    asset_ids: list[str],
    image_urls: list[str],
    user_notes: str = "",
) -> Listing:
    """
    Main orchestration function.

    Timeline (target < 5s):
    ┌────────────────────────────────────────────────────────────┐
    │ t=0   Upload complete; signed URLs ready                   │
    │ t=0   [PARALLEL] GPT-4o vision call starts                 │
    │ t=0   [PARALLEL] Replicate bg-removal starts               │
    │ t=2–4 GPT-4o response arrives → parse + validate           │
    │ t=3–5 Replicate response arrives (non-blocking for MVP)     │
    │ t=4   Compute fees + resolve shipping (local, ~0ms)         │
    │ t=4   Persist to Supabase (async, non-blocking to response) │
    │ t=4   Return Listing to client                              │
    └────────────────────────────────────────────────────────────┘

    Replicate's bg-removal result is persisted asynchronously and does NOT
    block the listing response. The frontend polls for the cleaned image
    separately via a /assets/{id}/bg-status endpoint.
    """
    start = time.monotonic()

    # Kick off GPT-4o and Replicate in parallel
    gpt_task        = asyncio.create_task(call_gpt4o_vision(image_urls, user_notes))
    replicate_tasks = [
        asyncio.create_task(remove_background_replicate(url))
        for url in image_urls
    ]

    # Await GPT-4o (critical path)
    try:
        ai_data = await gpt_task
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI analysis failed: {exc}")

    # Validate confidence; fall back gracefully
    confidence = ai_data.get("ai_confidence", 0.0)
    status     = "ready" if confidence >= 0.5 else "low_confidence"

    # Build price estimate
    raw_price = ai_data.get("price_estimate", {})
    price_est = PriceEstimate(
        fast_sale_pence   = raw_price.get("fast_sale_pence",   0),
        max_profit_pence  = raw_price.get("max_profit_pence",  0),
        recommended_pence = raw_price.get("recommended_pence"),
        confidence        = raw_price.get("confidence",        "low"),
        reasoning         = raw_price.get("reasoning",         ""),
        data_source       = "ai_estimate",
    )
    price_est = compute_platform_fees(price_est)

    # Override shipping with deterministic local logic (more reliable than model guess)
    weight_g   = ai_data.get("estimated_weight_g", 500)
    ship_local = resolve_shipping(weight_g)
    ship_est   = ShippingEstimate(**ship_local)

    # Build marketplace drafts
    drafts = [
        MarketplaceDraft(**d) for d in ai_data.get("drafts", [])
    ]

    # Bundle suggestion
    raw_bundle  = ai_data.get("bundle_suggestion", {})
    bundle_sugg = BundleSuggestion(**raw_bundle) if raw_bundle else BundleSuggestion(recommended=False)

    # Assemble canonical listing
    listing = Listing(
        user_id              = user_id,
        title                = drafts[0].title if drafts else ai_data.get("item_type", "Item"),
        item_type            = ai_data.get("item_type", "unknown"),
        category             = ai_data.get("category", ""),
        brand                = ai_data.get("brand"),
        model_style          = ai_data.get("model_style"),
        colour               = ai_data.get("colour", ""),
        material             = ai_data.get("material"),
        condition            = ai_data.get("condition", "good"),
        gender               = ai_data.get("gender", "unknown"),
        estimated_weight_g   = weight_g,
        ai_confidence        = confidence,
        processing_status    = status,
        is_bundle            = bundle_sugg.recommended,
        drafts               = drafts,
        price_estimate       = price_est,
        shipping_estimate    = ship_est,
        bundle_suggestion    = bundle_sugg,
        asset_ids            = asset_ids,
    )

    elapsed = time.monotonic() - start
    print(f"[ListingLift] orchestration completed in {elapsed:.2f}s")

    # Persist asynchronously — do NOT await; let it run in the background
    asyncio.create_task(_persist_listing(listing, ai_data, replicate_tasks))

    return listing


async def _persist_listing(
    listing: Listing,
    ai_raw: dict[str, Any],
    replicate_tasks: list[asyncio.Task],
) -> None:
    """
    Writes all listing data to Supabase. Runs in background after response sent.
    Errors are logged but do not affect the user-facing response.
    """
    try:
        # 1. Insert listing row
        row = listing.model_dump(
            exclude={"drafts","price_estimate","shipping_estimate","bundle_suggestion","asset_ids"}
        )
        row["ai_confidence"] = float(listing.ai_confidence)
        result = supabase.table("listings").insert(row).execute()
        listing_id = result.data[0]["id"]

        # 2. Insert marketplace drafts
        for draft in listing.drafts:
            supabase.table("marketplace_drafts").insert({
                "listing_id":      listing_id,
                "platform":        draft.platform,
                "title":           draft.title,
                "description":     draft.description,
                "hashtags":        draft.hashtags,
                "category_path":   draft.category_path,
                "condition_label": draft.condition_label,
                "keywords":        draft.keywords,
                "raw_json":        ai_raw,
            }).execute()

        # 3. Insert price estimate
        if listing.price_estimate:
            pe = listing.price_estimate.model_dump()
            pe["listing_id"] = listing_id
            supabase.table("price_estimates").insert(pe).execute()

        # 4. Insert shipping estimate
        if listing.shipping_estimate:
            se = listing.shipping_estimate.model_dump()
            se["listing_id"] = listing_id
            supabase.table("shipping_estimates").insert(se).execute()

        # 5. Insert bundle suggestion
        if listing.bundle_suggestion:
            bs = listing.bundle_suggestion.model_dump()
            bs["listing_id"] = listing_id
            supabase.table("bundle_suggestions").insert(bs).execute()

        # 6. Link assets to listing
        for asset_id in listing.asset_ids:
            supabase.table("uploaded_assets").update(
                {"listing_id": listing_id}
            ).eq("id", asset_id).execute()

        # 7. Await Replicate results and persist bg-removed paths
        for i, task in enumerate(replicate_tasks):
            try:
                bg_url = await asyncio.wait_for(task, timeout=30)
                if bg_url and i < len(listing.asset_ids):
                    supabase.table("uploaded_assets").update(
                        {"bg_removed_path": bg_url, "upload_status": "ready"}
                    ).eq("id", listing.asset_ids[i]).execute()
            except (asyncio.TimeoutError, Exception):
                pass  # Non-critical; UI degrades gracefully

    except Exception as exc:
        print(f"[ListingLift] persistence error: {exc}")


# =============================================================================
# FASTAPI APPLICATION
# =============================================================================

app = FastAPI(
    title        = "ListingLift API",
    description  = "AI-powered UK marketplace listing assistant",
    version      = "0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins     = ALLOWED_ORIGINS,
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)


class ScanRequest(BaseModel):
    user_id:   str
    asset_ids: list[str]
    image_urls: list[str]
    user_notes: str = ""


@app.post("/api/scan", response_model=Listing, summary="Analyse item and generate listings")
async def scan_item(req: ScanRequest) -> Listing:
    """
    Primary endpoint. Accepts pre-uploaded image URLs, runs analysis,
    and returns a complete Listing object within ~5 seconds.

    The frontend should:
    1. Upload images to Supabase Storage directly (pre-signed POST URL).
    2. Call this endpoint with the resulting asset IDs and signed URLs.
    3. Render the Profit Estimate Card immediately on response.
    4. Lazy-load the marketplace drafts as tabs below the card.
    """
    if not req.image_urls:
        raise HTTPException(status_code=400, detail="At least one image URL is required.")
    if len(req.image_urls) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 images per scan.")

    return await orchestrate_listing(
        user_id    = req.user_id,
        asset_ids  = req.asset_ids,
        image_urls = req.image_urls,
        user_notes = req.user_notes,
    )


@app.get("/api/listings/{listing_id}", response_model=Listing, summary="Fetch a saved listing")
async def get_listing(listing_id: str) -> Listing:
    """Retrieve a previously saved listing by ID."""
    result = (
        supabase.table("listings")
        .select("*")
        .eq("id", listing_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Listing not found.")
    return Listing(**result.data)


@app.get("/api/upload-url", summary="Get a pre-signed upload URL for Supabase Storage")
async def get_upload_url(user_id: str, filename: str) -> dict[str, str]:
    """
    Returns a pre-signed URL so the client can upload directly to Supabase Storage
    without routing bytes through this server. GDPR-friendly: images never touch
    our application server.
    """
    asset_id    = str(uuid.uuid4())
    storage_path = f"uploads/{user_id}/{asset_id}/{filename}"

    # Create the asset record first (status: pending)
    supabase.table("uploaded_assets").insert({
        "id":            asset_id,
        "user_id":       user_id,
        "storage_path":  storage_path,
        "upload_status": "pending",
    }).execute()

    # Get signed upload URL from Supabase Storage
    signed = supabase.storage.from_("item-images").create_signed_upload_url(storage_path)

    return {
        "asset_id":     asset_id,
        "upload_url":   signed["signedURL"],
        "storage_path": storage_path,
    }


@app.delete("/api/listings/{listing_id}", summary="Soft-delete a listing")
async def delete_listing(listing_id: str) -> dict[str, str]:
    """Marks listing as archived. Hard deletion handled by nightly GDPR job."""
    supabase.table("listings").update(
        {"processing_status": "archived"}
    ).eq("id", listing_id).execute()
    return {"status": "archived"}


# =============================================================================
# REACT PROFIT ESTIMATE CARD COMPONENT (TSX)
# =============================================================================

PROFIT_ESTIMATE_CARD_TSX = '''
// components/ProfitEstimateCard.tsx
// ListingLift — Profit Estimate Card
// Mobile-first · Claymorphism · Soft Utility aesthetic
// Tailwind CSS · React 18 · TypeScript

import React from "react";

// ─── Types ───────────────────────────────────────────────────────────────────

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
  carrier:             string;
  service:             string;
  estimatedCostPence:  number;
  weightBand:          string;
  reason:              string;
}

interface ProfitEstimateCardProps {
  title:            string;
  brand?:           string | null;
  condition:        string;
  aiConfidence:     number;            // 0–1
  priceEstimate:    PriceEstimate;
  shippingEstimate: ShippingEstimate;
  platform?:        "vinted" | "ebay_uk" | "depop";
  isLoading?:       boolean;
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

const pence = (p: number) =>
  new Intl.NumberFormat("en-GB", { style: "currency", currency: "GBP" }).format(p / 100);

const CONFIDENCE_COLOURS = {
  high:   "bg-mint-100 text-mint-800 border-mint-300",
  medium: "bg-amber-100 text-amber-800 border-amber-300",
  low:    "bg-rose-100  text-rose-800  border-rose-300",
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

const PLATFORM_FEES: Record<string, number> = {
  vinted:   0.05,
  ebay_uk:  0.128,
  depop:    0.10,
};

// ─── Sub-components ───────────────────────────────────────────────────────────

const ConfidenceBadge = ({ level }: { level: "high" | "medium" | "low" }) => (
  <span
    className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs
                font-semibold border ${CONFIDENCE_COLOURS[level]}`}
    aria-label={`Confidence: ${level}`}
  >
    <span className="w-1.5 h-1.5 rounded-full bg-current opacity-70" aria-hidden />
    {CONFIDENCE_LABELS[level]}
  </span>
);

const PriceBlock = ({
  label,
  amountPence,
  feePence,
  caption,
  highlight = false,
}: {
  label:       string;
  amountPence: number;
  feePence?:   number;
  caption:     string;
  highlight?:  boolean;
}) => (
  <div
    className={`flex-1 rounded-2xl p-4 flex flex-col gap-1 border
                ${highlight
                  ? "bg-mint-50 border-mint-300 shadow-inner"
                  : "bg-oatmeal-50 border-oatmeal-200"}`}
    aria-label={`${label}: ${pence(amountPence)}`}
  >
    <p className="text-xs font-medium text-neutral-500 uppercase tracking-wide">{label}</p>
    <p
      className={`text-2xl font-extrabold tabular-nums
                  ${highlight ? "text-mint-700" : "text-neutral-800"}`}
    >
      {pence(amountPence)}
    </p>
    {feePence !== undefined && (
      <p className="text-xs text-neutral-400">
        ~{pence(feePence)} platform fee
      </p>
    )}
    <p className={`text-xs mt-1 ${highlight ? "text-mint-600" : "text-neutral-500"}`}>
      {caption}
    </p>
  </div>
);

// ─── Main Component ───────────────────────────────────────────────────────────

export const ProfitEstimateCard: React.FC<ProfitEstimateCardProps> = ({
  title,
  brand,
  condition,
  aiConfidence,
  priceEstimate,
  shippingEstimate,
  platform = "ebay_uk",
  isLoading = false,
}) => {
  const feePence = platform === "ebay_uk"
    ? priceEstimate.ebayFeePence
    : platform === "depop"
    ? priceEstimate.depopFeePence
    : priceEstimate.vintedFeePence;

  const fastSaleCaption  = "Likely to sell fast";
  const maxProfitCaption = "Higher margin, slower sale";

  if (isLoading) {
    return (
      <div
        className="bg-white rounded-3xl shadow-clay p-5 flex flex-col gap-4 animate-pulse"
        aria-busy="true"
        aria-label="Analysing your item…"
      >
        <div className="h-5 bg-oatmeal-200 rounded-full w-3/4" />
        <div className="h-4 bg-oatmeal-200 rounded-full w-1/3" />
        <div className="flex gap-3">
          <div className="flex-1 h-24 bg-oatmeal-100 rounded-2xl" />
          <div className="flex-1 h-24 bg-mint-100 rounded-2xl" />
        </div>
        <div className="h-12 bg-oatmeal-100 rounded-2xl" />
        <p className="text-center text-sm text-neutral-400 font-medium">
          Sorting your clobber…
        </p>
      </div>
    );
  }

  return (
    <article
      className="bg-white rounded-3xl shadow-clay p-5 flex flex-col gap-4 w-full max-w-sm mx-auto"
      aria-label={`Profit estimate for ${title}`}
    >

      {/* ── Header ── */}
      <header className="flex items-start justify-between gap-2">
        <div className="flex flex-col gap-1">
          <h2 className="text-base font-bold text-neutral-900 leading-snug line-clamp-2">
            {title}
          </h2>
          <div className="flex items-center gap-2 flex-wrap">
            {brand && (
              <span className="text-xs bg-oatmeal-100 text-neutral-600 px-2 py-0.5 rounded-full border border-oatmeal-200">
                {brand}
              </span>
            )}
            <span className="text-xs text-neutral-500">
              {CONDITION_DISPLAY[condition] ?? condition}
            </span>
          </div>
        </div>
        <ConfidenceBadge level={priceEstimate.confidence} />
      </header>

      {/* ── Price Blocks ── */}
      <section aria-label="Price estimates" className="flex gap-3">
        <PriceBlock
          label="Fast Sale"
          amountPence={priceEstimate.fastSalePence}
          feePence={feePence}
          caption={fastSaleCaption}
        />
        <PriceBlock
          label="Max Profit"
          amountPence={priceEstimate.maxProfitPence}
          feePence={feePence}
          caption={maxProfitCaption}
          highlight
        />
      </section>

      {/* ── AI Confidence Bar ── */}
      <div aria-label={`AI confidence: ${Math.round(aiConfidence * 100)}%`}>
        <div className="flex justify-between text-xs text-neutral-400 mb-1">
          <span>AI confidence</span>
          <span>{Math.round(aiConfidence * 100)}%</span>
        </div>
        <div className="w-full h-2 bg-oatmeal-100 rounded-full overflow-hidden">
          <div
            className="h-full rounded-full bg-gradient-to-r from-mint-400 to-mint-600 transition-all"
            style={{ width: `${Math.round(aiConfidence * 100)}%` }}
            role="progressbar"
            aria-valuenow={Math.round(aiConfidence * 100)}
            aria-valuemin={0}
            aria-valuemax={100}
          />
        </div>
      </div>

      {/* ── Shipping Chip ── */}
      <div
        className="flex items-center gap-2 bg-oatmeal-50 border border-oatmeal-200
                   rounded-2xl px-4 py-3"
        aria-label={`Suggested shipping: ${shippingEstimate.service}`}
      >
        <span className="text-xl" aria-hidden>📦</span>
        <div className="flex flex-col gap-0.5 flex-1 min-w-0">
          <p className="text-xs font-semibold text-neutral-700 truncate">
            {shippingEstimate.service}
          </p>
          <p className="text-xs text-neutral-500">
            ~{pence(shippingEstimate.estimatedCostPence)} postage
          </p>
        </div>
        <span
          className="text-xs font-bold uppercase tracking-wider text-coral-600 bg-coral-50
                     border border-coral-200 px-2 py-0.5 rounded-full shrink-0"
        >
          {shippingEstimate.carrier === "royal_mail" ? "Royal Mail" : "Evri"}
        </span>
      </div>

      {/* ── Disclaimer ── */}
      <p className="text-[11px] text-neutral-400 leading-relaxed">
        Prices are AI estimates based on category, brand, and condition — not live
        market data. Platform fees shown are approximate. Check platform for current rates.
      </p>

      {/* ── CTA ── */}
      <button
        className="w-full bg-coral-500 hover:bg-coral-600 active:scale-95
                   text-white font-bold py-3.5 rounded-2xl text-sm tracking-wide
                   shadow-coral transition-all duration-150 focus-visible:outline-none
                   focus-visible:ring-2 focus-visible:ring-coral-400 focus-visible:ring-offset-2"
        aria-label="View full listing drafts"
      >
        View Listings — Sorted 🎉
      </button>

    </article>
  );
};

// ─── Tailwind config additions (tailwind.config.ts) ───────────────────────────
/*
theme: {
  extend: {
    colors: {
      mint:    { 50:"#f0fdf4", 100:"#dcfce7", 300:"#86efac", 400:"#4ade80",
                 600:"#16a34a", 700:"#15803d", 800:"#166534" },
      oatmeal: { 50:"#faf9f7",  100:"#f5f0e8", 200:"#e8e0d0" },
      coral:   { 50:"#fff5f5",  200:"#fecaca", 500:"#f87171", 600:"#ef4444" },
    },
    boxShadow: {
      clay:  "0 8px 32px rgba(0,0,0,0.08), 0 2px 8px rgba(0,0,0,0.04)",
      coral: "0 4px 12px rgba(248,113,113,0.35)",
    },
  },
},
*/
'''

# =============================================================================
# ARCHITECTURE NOTES
# =============================================================================

ARCHITECTURE_NOTES = """
╔══════════════════════════════════════════════════════════════╗
║           LISTINGLIFT — ARCHITECTURE DECISIONS              ║
╚══════════════════════════════════════════════════════════════╝

1. SERVER ACTIONS vs API ROUTES
   ─────────────────────────────
   • Next.js Server Actions handle the client-facing scan request.
     They stream a progress token to the UI (analysing → pricing → ready)
     via React's useOptimistic / streaming RSC, providing instant feedback.
   • This FastAPI backend sits behind the Next.js layer and can be
     deployed as a Supabase Edge Function (Deno/TypeScript port) for
     zero cold-start latency, or as a standalone Render/Railway service.
   • Use Next.js API routes ONLY for webhook endpoints (Replicate callbacks).

2. CACHING STRATEGY
   ─────────────────
   • GPT-4o responses are NOT cached (every photo is unique).
   • Shipping tier data is a static lookup — no cache needed.
   • Platform fee rates: cache in process memory, refresh daily.
   • Supabase query results: use Supabase's built-in read replica for
     listing history queries.
   • CDN (Vercel Edge): cache the /api/upload-url endpoint response for
     10 seconds per user to prevent duplicate asset records.

3. WHEN TO PERSIST DATA
   ──────────────────────
   • Listings are ALWAYS persisted (even low-confidence ones) so users
     can review and dismiss them.
   • Images are stored in Supabase Storage with a 30-day GDPR TTL unless
     the user explicitly taps "Save".
   • Background-removed images are stored alongside originals and linked
     via bg_removed_path.
   • Draft copy is persisted in marketplace_drafts immediately; the user
     can edit and re-save without re-scanning.

4. KEEPING RESPONSE TIME LOW
   ───────────────────────────
   a. Parallel execution: GPT-4o + Replicate start simultaneously.
   b. GPT-4o is the critical path. Replicate runs async and does NOT
      block the listing response. The UI renders with the original image
      first, then swaps to the bg-removed version when ready.
   c. Supabase persistence is fully async (fire-and-forget from user POV).
   d. Use GPT-4o with response_format: json_object + max_tokens: 2048 to
      minimise token streaming latency.
   e. Pre-sign upload URLs on the client side (Supabase JS SDK) so uploads
      go direct to storage — no proxy latency.

5. GDPR-SAFE DEFAULTS
   ─────────────────────
   • Images: 30-day auto-purge via a Supabase scheduled function.
   • No third-party analytics in the scan pipeline.
   • User email is never sent to OpenAI.
   • Image URLs sent to OpenAI are Supabase signed URLs that expire in 1h.
   • Signed URLs are never persisted in the DB; they are regenerated on read.
   • Privacy policy must disclose GPT-4o image processing (OpenAI DPA).

6. POST-MVP IMPROVEMENTS
   ─────────────────────────
   Priority 1 (Next sprint):
   • Live pricing: integrate with eBay Browse API for real sold-price comps.
   • Vinted API: auto-draft listings via Vinted's unofficial API (fragile;
     handle with care and user auth token).
   • Depop integration: deeplink with pre-filled listing fields.
   • Size detection: parse visible size labels from images.

   Priority 2 (Growth phase):
   • Batch mode: upload up to 30 items, get a full wardrobe clear plan.
   • Price history: track a listing's sell-through time and update pricing
     heuristics per user.
   • AI photo tips: detect poor lighting / bad angle and suggest retakes.
   • Push notifications: "Your listing has been live 7 days — lower the price?"

   Priority 3 (Monetisation):
   • Pro tier: unlimited scans, Replicate bg removal, CSV export.
   • Business tier: multi-account, team access, bulk listing to eBay via API.
   • Affiliate revenue: Royal Mail / Evri click-through shipping.
"""


# =============================================================================
# ENTRYPOINT
# =============================================================================

if __name__ == "__main__":
    import uvicorn

    print("╔══════════════════════════════════╗")
    print("║      ListingLift API Server      ║")
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
