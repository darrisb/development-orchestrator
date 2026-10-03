"""model_run cost snapshot (concern 78)

``model_runs`` recorded tokens and duration but nothing about money, so the
only way to answer what a run cost was to multiply its tokens by whatever the
model's price happens to be *today*. That calculation is wrong the first time
anyone corrects a price or a provider changes its rates: it reprices history.

Six columns make the cost a fact of the call rather than a derivation from
current configuration -- the prices in force at the time, and the arithmetic
done with them:

* ``pricing_currency`` -- the currency the two prices are quoted in. Costs in
  different currencies are never added; the aggregation groups by this.
* ``input_price_per_million`` / ``output_price_per_million`` -- the operator's
  configured prices as they stood when the call was made.
* ``input_cost`` / ``output_cost`` / ``total_cost`` -- the result. Stored
  rather than recomputed, which is the whole point of the revision.

NUMERIC(20, 12). The scale is twelve because costing divides a token count by
one million: a price quoted to six decimal places needs twelve to come out
exact, so at this scale the stored cost *is* the cost rather than a rounding of
it. Rounding each call to cents instead would lose most small calls entirely --
a 437-token completion can cost a thousandth of a cent -- and a run's total is
a sum of many such calls, so the error would be in the total and not just in
the parts. The precision of twenty leaves eight digits left of the point, which
is more dollars than any single model call will ever cost.

All six are nullable, and NULL means UNKNOWN: either the model has no pricing
configured or the endpoint reported no usage. That is deliberately distinct
from a cost of zero, which is what a model whose pricing explicitly declares
zero records, and which is a measurement rather than an absence.

**Nothing is backfilled.** Every row written before this revision gets NULL on
all six columns and keeps it. The existing rows carry token counts but no
record of what the model cost at the time, and applying today's prices to them
would manufacture figures that look like history and are not -- the one thing
the snapshot exists to prevent. An unpriced past call is therefore reported as
unknown-cost, which is true.

Revision ID: f7b2d4c80e13
Revises: d93f1a0c5b27
Create Date: 2026-10-03 09:40:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'f7b2d4c80e13'
down_revision = 'd93f1a0c5b27'
branch_labels = None
depends_on = None

#: Matches ``db.types.ExactDecimal``'s default. SQLite has no decimal storage
#: class, so there the value is kept as a fixed-scale string; see that type.
_MONEY = sa.Numeric(precision=20, scale=12)


def upgrade() -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    money = sa.String(23) if sqlite else _MONEY
    with op.batch_alter_table('model_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pricing_currency', sa.String(8), nullable=True))
        batch_op.add_column(
            sa.Column('input_price_per_million', money, nullable=True)
        )
        batch_op.add_column(
            sa.Column('output_price_per_million', money, nullable=True)
        )
        batch_op.add_column(sa.Column('input_cost', money, nullable=True))
        batch_op.add_column(sa.Column('output_cost', money, nullable=True))
        batch_op.add_column(sa.Column('total_cost', money, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('model_runs', schema=None) as batch_op:
        batch_op.drop_column('total_cost')
        batch_op.drop_column('output_cost')
        batch_op.drop_column('input_cost')
        batch_op.drop_column('output_price_per_million')
        batch_op.drop_column('input_price_per_million')
        batch_op.drop_column('pricing_currency')
