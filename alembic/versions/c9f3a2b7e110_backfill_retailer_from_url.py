"""Backfill retailer column from product URL domain

Revision ID: c9f3a2b7e110
Revises: 47b6bac2ab60
Create Date: 2026-07-22 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9f3a2b7e110'
down_revision: Union[str, Sequence[str], None] = '47b6bac2ab60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Back-fill the products.retailer column for any existing rows whose
    retailer is still the column default ('Unknown').

    Logic mirrors infer_retailer_from_url() in engines/ecommerce_extractors.py:
      url ILIKE '%amazon.com%'   -> 'amazon'
      url ILIKE '%walmart.com%'  -> 'walmart'
      url ILIKE '%bestbuy.com%'  -> 'bestbuy'

    Rows not matching any known domain are left as 'Unknown' — this is
    intentional; they will be corrected on the next run_monitor() call
    which re-upserts all products with inferred retailer values.
    """
    op.execute(
        """
        UPDATE products
        SET    retailer = CASE
                    WHEN url ILIKE '%amazon.com%'  THEN 'amazon'
                    WHEN url ILIKE '%walmart.com%' THEN 'walmart'
                    WHEN url ILIKE '%bestbuy.com%' THEN 'bestbuy'
                    ELSE retailer
               END
        WHERE  retailer = 'Unknown'
        """
    )


def downgrade() -> None:
    """
    Revert backfilled retailer values back to 'Unknown'.

    WARNING: this resets ALL inferred retailer labels, not just the ones
    that were 'Unknown' before the upgrade — there is no way to distinguish
    a backfilled 'amazon' from one that was set by the application before
    this migration ran.  Only use this if you need to fully reset the column.
    """
    op.execute(
        """
        UPDATE products
        SET    retailer = 'Unknown'
        WHERE  retailer IN ('amazon', 'walmart', 'bestbuy')
        """
    )
