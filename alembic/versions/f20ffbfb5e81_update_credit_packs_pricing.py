"""update_credit_packs_pricing

Revision ID: f20ffbfb5e81
Revises: 8b0199d7017b
Create Date: 2026-04-15 23:26:36.305675

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f20ffbfb5e81'
down_revision: Union[str, Sequence[str], None] = '8b0199d7017b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Old pack -> new pack mapping.
# The 10_pack is being replaced by 5_pack (removed, added new).
_PRICE_UPDATES = {
    "single": (1000, 500, 0, "1 estimate credit"),
    "25_pack": (12500, 7500, 40, "25 estimate credits (40% off)"),
    "50_pack": (20000, 10000, 60, "50 estimate credits (60% off)"),
    "100_pack": (30000, 17500, 65, "100 estimate credits (65% off)"),
    "250_pack": (50000, 37500, 70, "250 estimate credits (70% off)"),
}


def upgrade() -> None:
    conn = op.get_bind()

    # Update existing packs with new prices.
    for pack_key, (old_price, new_price, discount, desc) in _PRICE_UPDATES.items():
        conn.execute(
            sa.text(
                "UPDATE credit_packs "
                "SET price_cents = :price_cents, discount_pct = :discount_pct, "
                "    description = :description "
                "WHERE pack_key = :pack_key"
            ),
            {
                "pack_key": pack_key,
                "price_cents": new_price,
                "discount_pct": discount,
                "description": desc,
            },
        )

    # Deactivate old 10-pack and insert new 5-pack.
    conn.execute(
        sa.text("UPDATE credit_packs SET is_active = FALSE WHERE pack_key = '10_pack'")
    )
    conn.execute(
        sa.text(
            "INSERT INTO credit_packs "
            "(pack_key, name, credits, price_cents, discount_pct, description, "
            " stripe_product_id, stripe_price_id, is_active, is_featured, sort_order) "
            "SELECT '5_pack', '5-Pack', 5, 2000, 20, '5 estimate credits (20% off)', "
            "       '', '', TRUE, FALSE, 15 "
            "WHERE NOT EXISTS (SELECT 1 FROM credit_packs WHERE pack_key = '5_pack')"
        ),
    )

    # Fix sort orders to match new lineup.
    sort_orders = {"single": 0, "5_pack": 15, "25_pack": 30, "50_pack": 40, "100_pack": 50, "250_pack": 60}
    for pack_key, sort_order in sort_orders.items():
        conn.execute(
            sa.text(
                "UPDATE credit_packs SET sort_order = :sort_order WHERE pack_key = :pack_key"
            ),
            {"pack_key": pack_key, "sort_order": sort_order},
        )


def downgrade() -> None:
    conn = op.get_bind()

    # Restore old 10-pack prices.
    old_prices = {
        "single": (1000, 0, "1 estimate credit"),
        "10_pack": (6000, 40, "10 estimate credits (40% off)"),
        "25_pack": (12500, 50, "25 estimate credits (50% off)"),
        "50_pack": (20000, 60, "50 estimate credits (60% off)"),
        "100_pack": (30000, 70, "100 estimate credits (70% off)"),
        "250_pack": (50000, 80, "250 estimate credits (80% off)"),
    }
    for pack_key, (price, discount, desc) in old_prices.items():
        conn.execute(
            sa.text(
                "UPDATE credit_packs "
                "SET price_cents = :price_cents, discount_pct = :discount_pct, "
                "    description = :description, is_active = TRUE "
                "WHERE pack_key = :pack_key"
            ),
            {"pack_key": pack_key, "price_cents": price, "discount_pct": discount, "description": desc},
        )

    # Remove 5-pack.
    conn.execute(
        sa.text("DELETE FROM credit_packs WHERE pack_key = '5_pack'")
    )
