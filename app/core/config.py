"""Environment configuration.

Loads and validates every environment variable the app needs (Section 7 of the build
plan). Nothing else in the app reads ``os.environ`` directly — modules import
``settings`` from here so there is exactly one place where configuration is defined.
"""

from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository root (two levels up from app/core/config.py).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"


class Settings(BaseSettings):
    """All runtime configuration, read from the environment or a local ``.env`` file."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App -------------------------------------------------------------
    app_name: str = "Wanas Gallery Chatbot"
    environment: str = "development"
    debug: bool = True

    # --- Gemini ----------------------------------------------------------
    # gemini-3.7-flash is the current latest stable Flash model on the free tier
    # (verified against the Models API on 2026-08-17). Supports function calling and
    # native image input, which this project needs for tools and image matching.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.7-flash"
    # Tried in order when the primary model is rate-limited or returns 503.
    # Each model carries its OWN ~20-request free-tier budget (measured 2026-08-17), so a
    # wider chain multiplies the effective allowance. All of these are verified to
    # support tool calling and native image input.
    gemini_fallback_models: List[str] = [
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-2.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ]
    # "low" keeps replies fast for chat; "medium"/"high" also accepted. "minimal" is not
    # supported by the 3.x flash models (verified 2026-08-17).
    # Which model matches a photo against the catalog. Empty means "whatever answers the
    # conversation", which is usually right - set it only when the chat model is chosen
    # for speed and turns out to be weaker at recognising a garment.
    gemini_vision_model: str = ""
    # Which model listens to voice notes. Empty means "whatever answers the
    # conversation", which on a single-model store means a voice turn costs two of that
    # one model's requests. Pointing this at a different model gives transcription its
    # own free-tier budget, which is why the vision model is set the same way.
    gemini_transcription_model: str = ""
    # Which model decides what a public comment is. Its own budget again, and for a
    # sharper reason than the others: classifying must not be able to starve replying,
    # because a comment arrives whether or not a customer is mid-conversation.
    gemini_classifier_model: str = ""
    gemini_thinking_level: str = "low"
    gemini_temperature: float = 0.4

    # --- Conversation -----------------------------------------------------
    # How many past messages of a conversation to replay into the model each turn.
    chat_history_limit: int = 20
    # Cap on model->tool->model rounds within a single turn, so a model that keeps
    # calling tools cannot loop forever or drain the free-tier quota.
    max_tool_rounds: int = 4

    # --- Shopify ---------------------------------------------------------
    # Store domain, e.g. "wanas-gallery.myshopify.com" (with or without protocol).
    shopify_store: str = ""
    shopify_access_token: str = ""
    shopify_api_version: str = "2026-07"
    shopify_timeout_seconds: float = 30.0
    # How long a fetched catalog stays usable before it is refetched. The catalog is
    # small and changes rarely, so this mostly saves latency on repeated searches.
    catalog_cache_seconds: float = 300.0

    # --- Orders ----------------------------------------------------------
    # Order numbers are sequential and easy to guess, and an order record carries a
    # name, a phone number and a delivery city. With this on, a customer asking about an
    # order must also give the email or phone recorded on it. Turning it off makes any
    # order readable by number alone - only sensible for a closed test store.
    orders_require_contact_verification: bool = True

    # Cash on delivery. These tags are the store's existing convention, confirmed by
    # the owner - orders placed by the previous bot carry them, so staff already filter
    # on them. The channel the conversation came from is appended at creation time.
    cod_order_tags: List[str] = ["cash-on-delivery", "chatbot"]
    # BYPASS would let the shop sell stock it does not have; obeying the policy makes an
    # order fail rather than oversell.
    cod_inventory_behaviour: str = "DECREMENT_OBEYING_POLICY"
    # Whether Shopify emails the customer a confirmation. On, at the store owner's
    # request (2026-08-18). Most COD customers give only a phone, in which case Shopify
    # has no address to send to and nothing goes out.
    cod_send_receipt: bool = True
    # An identical order for the same phone inside this window is treated as a repeat
    # call rather than a second order. 0 disables the check.
    cod_duplicate_window_seconds: float = 900.0
    # Used for the shipping address country, and to put local phone numbers into the
    # international form Shopify requires: it rejects "01000000000" outright.
    store_country_code: str = "EG"
    store_dial_code: str = "20"
    store_currency: str = "EGP"
    # Delivery is read from the store's own shipping rates (needs the read_shipping
    # scope), so changing a rate in the Shopify admin is enough. These two are only a
    # fallback for when the rate cannot be read at all; None means no delivery line.
    cod_shipping_fee: Optional[float] = None
    cod_shipping_title: str = "Delivery"
    # Rates change far less often than stock.
    shipping_cache_seconds: float = 3600.0

    # --- Online payment ---------------------------------------------------
    # The other way to pay: a draft order with a Shopify checkout link the customer
    # opens themselves. The bot never sees a card number - the whole point of handing
    # over a link instead of taking details in chat.
    #
    # Verified 2026-08-19: a draft's invoiceUrl reaches the checkout even while the
    # storefront is password-protected, so this works before the shop is published.
    online_payment_enabled: bool = True
    # No "cash-on-delivery" here - that tag means cash to collect, and these are paid
    # online. The channel is appended at creation time, as it is for COD.
    online_order_tags: List[str] = ["online-payment", "chatbot"]
    # A repeat call for the same basket in the same conversation reuses the link it
    # already gave rather than issuing a second one - two live links for one basket
    # invite paying twice. There is no time window on that, deliberately: an old link
    # still takes money. What ends the reuse is the price changing.

    # --- Voice notes ------------------------------------------------------
    # Customers here talk more readily than they type, so a spoken message is a first
    # class one: it is transcribed and then treated exactly like something typed.
    voice_notes: bool = True

    # --- Instagram --------------------------------------------------------
    # Comments on posts and reels, and the DM conversation a comment can open. Meta
    # calls the DM half "Private Replies to Comments" - it is a sanctioned API, not a
    # workaround, but it only exists for Business/Creator accounts on a reviewed app.
    #
    # All three of token, account id and app secret are needed before anything runs:
    # the token to act, the account id to recognise our own comments and echoes (without
    # it the bot answers itself), and the secret to prove an event really came from Meta.
    instagram_access_token: str = ""
    instagram_business_account_id: str = ""
    instagram_app_secret: str = ""
    # Echoed back during Meta's webhook subscription handshake. Any long random string.
    instagram_webhook_verify_token: str = ""
    instagram_graph_version: str = "v25.0"
    instagram_timeout_seconds: float = 20.0

    # The master switch. Off means the webhook still verifies and still returns 200 -
    # Meta disables a subscription that errors - but nothing is classified or sent.
    instagram_engagement_enabled: bool = True
    # Classify, decide, log - and send nothing at all. On by default, deliberately: the
    # first run against real comments must be readable before it is public. Turning this
    # off is a decision someone makes after reading that log.
    instagram_dry_run: bool = True
    # Whether the public "we have sent you a DM" reply is posted. The private reply and
    # the like are unaffected.
    instagram_public_replies: bool = True
    # Self-throttle. Meta publishes far higher ceilings (750 private replies/hour), but
    # a burst of automated public activity reads as spam whatever the limit says, and a
    # runaway loop is cheaper to notice at 200/hour than at 750.
    instagram_max_actions_per_hour: int = 200
    # Instagram truncates a DM past this, so a long reply is split rather than cut.
    instagram_message_char_limit: int = 1000

    # --- Database --------------------------------------------------------
    # Unset locally -> SQLite file under ./data. Set to a Postgres URL in production.
    database_url: Optional[str] = None

    # --- Support tickets --------------------------------------------------
    # The same issue raised twice in one conversation is one issue. Stops a customer
    # restating a complaint - or the model calling twice - from filling the owner's inbox.
    ticket_duplicate_window_seconds: float = 1800.0

    # --- Feedback ---------------------------------------------------------
    # One conversation is one opinion. A customer who says thank you twice has not
    # changed their mind, and the owner should not read it twice.
    feedback_duplicate_window_seconds: float = 1800.0

    # --- How long delivery takes ------------------------------------------
    # Shopify does not carry this. Checked 2026-08-19: the store has one Domestic zone
    # covering all 29 governorates with a single rate named "قياسي", whose description
    # is empty - there is no delivery-time field to read. So the owner states it here.
    #
    # The same for every governorate, by the owner's answer. Leave both at 0 and the bot
    # says nothing about timing rather than guessing - which is the rule that exists
    # because it once invented delivery dates.
    delivery_days_min: int = 0
    delivery_days_max: int = 0
    # Whether those are working days (Sun-Thu here) or calendar days. Changes the words
    # the bot uses, nothing else.
    delivery_working_days: bool = True

    # --- Owner dashboard --------------------------------------------------
    # The dashboard shows customer names, phones, delivery addresses and whole
    # transcripts, so it is off unless a token is set - never open by default. Set it to
    # a long random string; it is compared in constant time.
    dashboard_token: str = ""

    # --- Owner dashboard (admin: accounts, analytics, conversations) ------
    # A separate, account-based login for the new React dashboard (Section 5 of
    # owner-dashboard-plan.md) - independent of the single shared-secret /dashboard
    # view above. How long a login stays valid before the browser must sign in again.
    admin_session_ttl_hours: float = 168.0
    # Optional: creates the first owner account from the environment at startup, so
    # there is a way in before any account exists. Idempotent - only acts if this
    # username is not already taken, so it is safe to leave set permanently and never
    # overwrites a password since changed through the dashboard.
    admin_owner_username: str = ""
    admin_owner_password: str = ""

    # --- Email notifications --------------------------------------------
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_pass: Optional[str] = None
    smtp_from: Optional[str] = None
    store_owner_email: Optional[str] = None
    # Port 587 with STARTTLS is the common setup; port 465 wants SSL from the start.
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    # A slow mail server must not hold up a customer's reply.
    smtp_timeout_seconds: float = 15.0

    @field_validator("shopify_store")
    @classmethod
    def _normalise_store(cls, value: str) -> str:
        """Accept a bare domain or a full URL; store the bare host."""
        cleaned = value.strip().removeprefix("https://").removeprefix("http://")
        return cleaned.rstrip("/")

    # --- Derived values --------------------------------------------------
    @property
    def sqlalchemy_url(self) -> str:
        """Database URL to hand to SQLAlchemy, defaulting to a local SQLite file."""
        if self.database_url:
            return self.database_url
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(DATA_DIR / 'wanas.db').as_posix()}"

    @property
    def shopify_admin_base_url(self) -> str:
        """Base URL for Shopify Admin API calls."""
        return f"https://{self.shopify_store}/admin/api/{self.shopify_api_version}"

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def shopify_configured(self) -> bool:
        return bool(self.shopify_store and self.shopify_access_token)

    @property
    def email_configured(self) -> bool:
        """Email notifications need a server, a sender and a recipient to be useful."""
        return bool(self.smtp_host and self.smtp_from and self.store_owner_email)

    @property
    def voice_notes_enabled(self) -> bool:
        """Whether the assistant will listen to recordings at all.

        Needs Gemini, which is what does the listening. Off means the widget hides its
        record button and the endpoint refuses audio.
        """
        return self.voice_notes and self.gemini_configured

    @property
    def online_payment_configured(self) -> bool:
        """Whether the bot may offer a payment link at all.

        Needs Shopify, because the link is a Shopify checkout. Off means the bot offers
        cash on delivery only, and the tool is not even declared to the model.
        """
        return self.online_payment_enabled and self.shopify_configured

    @property
    def instagram_configured(self) -> bool:
        """Whether Instagram can be talked to at all.

        Fails closed like ``dashboard_enabled``: a half-configured integration would
        act in public with no way to check an event was genuine, so all three of the
        token, our own account id and the app secret are required together.
        """
        return bool(self.instagram_access_token
                    and self.instagram_business_account_id
                    and self.instagram_app_secret)

    @property
    def instagram_enabled(self) -> bool:
        """Whether comments and DMs are actually acted on.

        Needs Gemini too - the DM half is the ordinary assistant, and the comment half
        cannot decide what a comment is without a model.
        """
        return (self.instagram_engagement_enabled
                and self.instagram_configured
                and self.gemini_configured)

    @property
    def delivery_period_known(self) -> bool:
        """Whether there is a real delivery window to quote."""
        return self.delivery_days_min > 0 and self.delivery_days_max >= self.delivery_days_min

    @property
    def dashboard_enabled(self) -> bool:
        """Off unless a token is set. A short token is treated as no token.

        Fails closed on purpose: everything behind the dashboard is customer personal
        data, so the accident of an unset variable must not publish it.
        """
        return len(self.dashboard_token.strip()) >= 16

    def missing_required(self) -> List[str]:
        """Config the app cannot work at all without."""
        missing = []
        if not self.gemini_api_key:
            missing.append("GEMINI_API_KEY")
        return missing

    def missing_optional(self) -> List[str]:
        """Config that only disables part of the app when absent (logged at startup)."""
        missing = []
        if not self.shopify_store:
            missing.append("SHOPIFY_STORE")
        if not self.shopify_access_token:
            missing.append("SHOPIFY_ACCESS_TOKEN")
        if not self.email_configured:
            missing.append("SMTP_HOST/SMTP_FROM/STORE_OWNER_EMAIL")
        return missing


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor so the ``.env`` file is parsed once per process."""
    return Settings()


settings = get_settings()
