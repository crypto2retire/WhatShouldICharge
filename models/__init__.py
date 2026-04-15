from database import Base
from sqlalchemy import Column, Integer, Float, DateTime, Text, String, Boolean, ForeignKey
from datetime import datetime, timedelta, timezone


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    company_name = Column(String, default="")
    company_city = Column(String, default="")
    company_state = Column(String, default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    subscription_tier = Column(String, default="free", index=True)
    estimates_used = Column(Integer, default=0)
    estimates_limit = Column(Integer, default=3)
    stripe_customer_id = Column(String, default="")
    stripe_subscription_id = Column(String, default="")
    price_per_cy_low = Column(Float, default=35.0)
    price_per_cy_high = Column(Float, default=40.0)
    price_per_cy_premium = Column(Float, default=55.0)
    min_charge = Column(Float, default=75.0)
    truck_capacity_cy = Column(Float, default=16.0)
    is_admin = Column(Boolean, default=False)
    company_slug = Column(String, default="", index=True)
    company_phone = Column(String, default="")
    company_logo_url = Column(String, default="")
    price_per_cy_standard = Column(Float, default=None)
    price_per_cy_heavy = Column(Float, default=None)
    is_active = Column(Boolean, default=True)
    admin_notes = Column(Text, default="")
    timezone = Column(String(50), default="America/Chicago")
    monthly_call_limit = Column(Integer, default=150)
    monthly_calls_used = Column(Integer, default=0)
    billing_cycle_start = Column(DateTime, default=None)
    overage_mode = Column(String(20), default="warn_and_charge")
    overage_cap_cents = Column(Integer, default=0)
    overage_charges_cents = Column(Integer, default=0)
    role = Column(String(20), default="owner")
    industry = Column(String, default="junk_removal")
    credit_balance = Column(Integer, default=0)
    credits_purchased_total = Column(Integer, default=0)
    credits_used_total = Column(Integer, default=0)
    free_trial_used = Column(Integer, default=0)
    free_trial_email = Column(String, default="")
    google_tag_id = Column(String, default="")
    fb_pixel_id = Column(String, default="")


class TeamMember(Base):
    __tablename__ = "team_members"
    id = Column(Integer, primary_key=True, index=True)
    owner_user_id = Column(Integer, nullable=False, index=True)
    name = Column(String, nullable=False)
    pin_hash = Column(String, nullable=False)
    role = Column(String, default="estimator")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))


class TeamSession(Base):
    __tablename__ = "team_sessions"
    id = Column(Integer, primary_key=True, index=True)
    team_member_id = Column(Integer, nullable=False)
    owner_user_id = Column(Integer, nullable=False)
    token = Column(String, unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    expires_at = Column(DateTime, default=None)


class SiteConfig(Base):
    __tablename__ = "site_config"
    id = Column(Integer, primary_key=True, index=True)
    config_key = Column(String, unique=True, nullable=False, index=True)
    config_value = Column(Text, default="")
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))


class PlanConfig(Base):
    __tablename__ = "plan_configs"
    id = Column(Integer, primary_key=True, index=True)
    tier_name = Column(String, unique=True)
    display_name = Column(String, default="")
    price_cents = Column(Integer, default=0)
    estimate_limit = Column(Integer, default=3)
    features_json = Column(Text, default="[]")
    stripe_price_id = Column(String(120), default="")
    is_active = Column(Boolean, default=True)


class CreditPack(Base):
    __tablename__ = "credit_packs"
    id = Column(Integer, primary_key=True, index=True)
    pack_key = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=False)
    credits = Column(Integer, nullable=False)
    price_cents = Column(Integer, nullable=False)
    discount_pct = Column(Integer, default=0)
    description = Column(Text, default="")
    stripe_product_id = Column(String, default="")
    stripe_price_id = Column(String, default="")
    is_active = Column(Boolean, default=True)
    is_featured = Column(Boolean, default=False)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None))


class CreditTransaction(Base):
    __tablename__ = "credit_transactions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    transaction_type = Column(String, nullable=False)
    credits = Column(Integer, nullable=False)
    balance_after = Column(Integer, nullable=False)
    description = Column(String, default="")
    stripe_session_id = Column(String, default="")
    pack_type = Column(String, default="")
    amount_cents = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), index=True)


class PromoCode(Base):
    __tablename__ = "promo_codes"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, nullable=False, index=True)
    discount_type = Column(String(20), nullable=False)
    discount_value = Column(Float, nullable=False)
    applies_to = Column(Text, default='{"products":["all"]}')
    usage_limit = Column(Integer, default=0)
    times_used = Column(Integer, default=0)
    expires_at = Column(DateTime, default=None)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))


class Session(Base):
    __tablename__ = "sessions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False)
    token = Column(String, unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    expires_at = Column(DateTime)


class PasswordReset(Base):
    __tablename__ = "password_resets"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    token_hash = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, default=None)


class Estimate(Base):
    __tablename__ = "estimates"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, default=0, index=True)
    team_member_id = Column(Integer, default=0, index=True)
    estimate_name = Column(String, default="")
    customer_name = Column(String, default="")
    customer_email = Column(String, default="")
    customer_phone = Column(String, default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), index=True)
    photos_count = Column(Integer)
    result_json = Column(Text)
    price_low = Column(Float)
    price_high = Column(Float)
    cy_estimate = Column(Float)
    pass1_json = Column(Text, default="")
    pass2_json = Column(Text, default="")
    lookups_json = Column(Text, default="")
    photos_json = Column(Text, default="")
    actual_price = Column(Float, default=None)
    actual_cy = Column(Float, default=None)
    actual_truck_fraction = Column(Float, default=None)
    accuracy_notes = Column(Text, default="")
    correction_reason = Column(String(40), default="")
    preferred_contact = Column(String, default="phone")
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    api_cost_cents = Column(Integer, default=0)
    model_used = Column(String(50), default="")
    capture_mode = Column(String(30), default="remote")
    confidence_bucket = Column(String(20), default="")
    confidence_reasons = Column(Text, default="")
    photo_quality_flags = Column(Text, default="")
    scene_type = Column(String(50), default="")
    occupancy_class = Column(String(30), default="")
    sanity_flags = Column(Text, default="")
    geometry_summary = Column(Text, default="")
    review_status = Column(String(30), default="auto_approved")
    review_reason = Column(Text, default="")
    appointment_requested = Column(Boolean, default=False)
    appointment_contact_method = Column(String, default="")
    appointment_preferred_day = Column(String, default="")
    appointment_preferred_time = Column(String, default="")
    appointment_requested_at = Column(DateTime, default=None)
    additional_items_text = Column(Text, default="")
    adjustments_json = Column(Text, default="")


class ProviderHealthEvent(Base):
    __tablename__ = "provider_health_events"
    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), index=True)
    provider_name = Column(String(30), default="", index=True)
    model_name = Column(String(80), default="")
    status = Column(String(20), default="", index=True)
    error_type = Column(String(60), default="")
    error_message = Column(Text, default="")
    estimate_job_id = Column(String(64), default="", index=True)
    estimate_id = Column(Integer, default=0, index=True)
    photos_count = Column(Integer, default=0)
    latency_ms = Column(Integer, default=0)


class ItemReferenceLibrary(Base):
    __tablename__ = "item_reference_library"
    id = Column(Integer, primary_key=True, index=True)
    item_name = Column(String, unique=True, nullable=False, index=True)
    item_category = Column(String, default="")
    cubic_yards = Column(Float, default=0.0)
    dimensions = Column(Text, default="")
    is_special = Column(Boolean, default=False)
    special_fee = Column(Float, default=0.0)
    confidence = Column(Float, default=0.8)
    source = Column(String, default="seed")
    search_query_used = Column(String, default="")
    times_seen = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
