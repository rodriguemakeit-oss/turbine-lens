"""Rates are unknown until configured for your own deployment."""
from __future__ import annotations
import logging
from dataclasses import dataclass
from ..config import SQL_WAREHOUSE_ID
logger = logging.getLogger(__name__)
MEASURED = 'measured'
LIST_PRICE = 'list_price'
PUBLISHED = 'published'
CONFIG = 'config'
ASSUMPTION = 'assumption'
UNKNOWN = 'unknown'

@dataclass(frozen=True)
class Rate:
    value: float | None
    source: str
    as_of: str = ''
    note: str = ''

    def as_dict(self) -> dict:
        return {'value': self.value, 'source': self.source, 'as_of': self.as_of, 'note': self.note}
PRICE_SEEDS: dict[str, Rate] = {'usd_per_dbu_sql': Rate(None, UNKNOWN, '', 'Configure with your own rates and usage.'), 'usd_per_dbu_apps': Rate(None, UNKNOWN, '', 'Configure with your own rates and usage.'), 'usd_per_dbu_genie': Rate(None, UNKNOWN, '', 'Configure with your own rates and usage.'), 'dbu_per_hr_sql': Rate(None, UNKNOWN, '', 'Configure with your own rates and usage.'), 'dbu_per_hr_apps': Rate(None, UNKNOWN, '', 'Configure with your own rates and usage.'), 'usd_per_cu_hr_lakebase': Rate(None, UNKNOWN, '', 'Configure with your own rates and usage.')}
COMMERCIAL_SEEDS: dict[str, Rate] = {'discount_a_pct': Rate(None, UNKNOWN, '', 'Configure with your own rates and usage.'), 'discount_b_pct': Rate(None, UNKNOWN, '', 'Configure with your own rates and usage.')}
MEASURED_SEEDS: dict[str, Rate] = {'usd_per_user_month': Rate(None, UNKNOWN, '', 'Configure with your own rates and usage.'), 'genie_dbu_per_user_month': Rate(None, UNKNOWN, '', 'Configure with your own rates and usage.'), 'turns_per_user_month': Rate(None, UNKNOWN, '', 'Configure with your own rates and usage.')}
CONFIG_FACTS: dict[str, Rate] = {'sql_warehouse_id': Rate(None, CONFIG, '', f'{SQL_WAREHOUSE_ID} (server/config.py)'), 'app_compute_size': Rate(None, CONFIG, '', 'LARGE (resources/turbine_lens_mvp.app.yml)')}
REGION_SUFFIX = 'EUROPE_IRELAND'
_PRICE_PATTERNS = {'usd_per_dbu_sql': ('SERVERLESS_SQL_COMPUTE',), 'usd_per_dbu_apps': ('ALL_PURPOSE_SERVERLESS_COMPUTE',), 'usd_per_dbu_genie': ('SERVERLESS_REAL_TIME_INFERENCE',), 'usd_per_cu_hr_lakebase': ('DATABASE_SERVERLESS_COMPUTE',)}

def _live_prices() -> dict[str, Rate]:
    statement = "\n        SELECT sku_name, pricing.default AS usd\n        FROM system.billing.list_prices\n        WHERE currency_code = 'USD' AND price_end_time IS NULL\n    "
    try:
        from databricks.sdk import WorkspaceClient
        resp = WorkspaceClient().statement_execution.execute_statement(warehouse_id=SQL_WAREHOUSE_ID, statement=statement, wait_timeout='30s')
        rows = (resp.result.data_array if resp.result else None) or []
    except Exception:
        logger.info('live list_prices unavailable — using seeded rates', exc_info=True)
        return {}
    found: dict[str, Rate] = {}
    for key, patterns in _PRICE_PATTERNS.items():
        for sku, usd in rows:
            name = (sku or '').upper()
            if REGION_SUFFIX not in name or usd is None:
                continue
            if any((p in name for p in patterns)):
                found[key] = Rate(float(usd), LIST_PRICE, 'live', f'system.billing.list_prices, SKU {sku}')
                break
    logger.info('live list_prices resolved %d of %d keys', len(found), len(_PRICE_PATTERNS))
    return found

def resolve(*, live: bool=True) -> dict:
    rates: dict[str, Rate] = {**PRICE_SEEDS, **MEASURED_SEEDS, **COMMERCIAL_SEEDS}
    if live:
        for key, rate in _live_prices().items():
            if key == 'usd_per_cu_hr_lakebase' and rate.value is not None:
                continue  # A DBU-to-CU conversion needs your own measured configuration.
            rates[key] = rate
    missing = sorted((k for k, r in rates.items() if r.value is None))
    return {'rates': {k: r.as_dict() for k, r in rates.items()}, 'config': {k: r.as_dict() for k, r in CONFIG_FACTS.items()}, 'missing': missing, 'ready': not missing}

def values(resolved: dict) -> dict:
    return {k: v.get('value') for k, v in (resolved.get('rates') or {}).items()}
