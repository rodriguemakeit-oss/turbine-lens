"""Cost scenarios. Inputs are assumptions until configured and measured."""
from __future__ import annotations
import math
HOURS_PER_MONTH = 720.0
MEASURED_WORKING_DAYS = 22.0
GENIE_FREE_DBU_PER_USER = 150.0
LINE_DISCOUNT: dict[str, str | None] = {'llm': None, 'genie': 'discount_b_pct', 'sql': 'discount_a_pct', 'apps': 'discount_a_pct', 'lakebase': 'discount_b_pct'}
DEFAULT_ASSUMPTIONS: dict[str, float] = {'peak_hours_per_day': 4.0, 'working_hours_per_day': 24.0, 'working_days_per_month': 22.0, 'sql_hours_per_day': 6.2, 'users_per_instance': 10.0, 'users_per_cu': 20.0, 'queries_in_flight_per_user': 2.0, 'queries_per_cluster': 10.0, 'min_clusters': 1.0, 'min_instances': 1.0, 'min_cu': 1.0}

def _f(source: dict, key: str, default: float=0.0) -> float:
    raw = (source or {}).get(key, default)
    if isinstance(raw, dict):
        raw = raw.get('value', default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)

def day_shape(assumptions: dict, *, has_demand: bool=True) -> dict:
    peak_per_day = max(0.0, min(24.0, _f(assumptions, 'peak_hours_per_day')))
    days = max(0.0, min(31.0, _f(assumptions, 'working_days_per_month')))
    sql_per_day = max(0.0, min(24.0, _f(assumptions, 'sql_hours_per_day')))
    work_per_day = max(0.0, min(24.0, _f(assumptions, 'working_hours_per_day', 24.0)))
    peak_per_day = min(peak_per_day, work_per_day)
    sql_per_day = min(sql_per_day, work_per_day)
    if not has_demand:
        sql_per_day = 0.0
    peak_hours = peak_per_day * days
    working_hours = work_per_day * days
    sql_awake = sql_per_day * days
    sql_peak = min(peak_hours, sql_awake)
    return {'peak_hours_month': peak_hours, 'working_hours_month': working_hours, 'in_hours_offpeak': max(0.0, working_hours - peak_hours), 'off_hours_month': max(0.0, HOURS_PER_MONTH - working_hours), 'off_peak_247': max(0.0, HOURS_PER_MONTH - peak_hours), 'sql_awake_month': sql_awake, 'sql_peak_hours': sql_peak, 'sql_off_peak': max(0.0, sql_awake - sql_peak)}

def _clamp_baselines(assumptions: dict) -> dict:
    out = dict(assumptions or {})
    for key in ('min_instances', 'min_cu', 'in_hours_instances', 'in_hours_cu'):
        out[key] = max(0.0, _f(assumptions, key, 1.0))
    out['min_clusters'] = max(1.0, _f(assumptions, 'min_clusters', 1.0))
    return out

def _units(concurrent: float, per_unit: float, multiplier: float=1.0) -> int:
    if concurrent <= 0:
        return 0
    if per_unit <= 0:
        return 0
    return int(math.ceil(concurrent * multiplier / per_unit))

def _next_step(concurrent: float, per_unit: float, multiplier: float=1.0) -> int | None:
    if per_unit <= 0 or multiplier <= 0:
        return None
    here = _units(concurrent, per_unit, multiplier)
    ceiling = int(max(1.0, concurrent)) + int(math.ceil(per_unit / multiplier)) + 2
    for candidate in range(max(1, int(math.floor(concurrent)) + 1), ceiling + 1):
        if _units(candidate, per_unit, multiplier) > here:
            return candidate
    return None

def forecast(users: float, concurrent: float, assumptions: dict, rates: dict) -> dict:
    users = max(0.0, float(users or 0))
    concurrent = max(0.0, float(concurrent or 0))
    a = _clamp_baselines({**DEFAULT_ASSUMPTIONS, **(assumptions or {})})
    hours = day_shape(a, has_demand=concurrent > 0)
    lines: list[dict] = []
    notes: list[str] = []
    activity = _f(a, 'working_days_per_month') / MEASURED_WORKING_DAYS if MEASURED_WORKING_DAYS else 0.0
    if _f(a, 'working_hours_per_day', 24.0) <= 0:
        activity = 0.0
    llm_per_user = _f(rates, 'usd_per_user_month')
    lines.append({'key': 'llm', 'label': 'Agent LLM (Claude via FMAPI)', 'driver': 'users', 'usd': users * llm_per_user * activity, 'formula': 'users × $/user/month × (working days ÷ 22 measured)', 'detail': {'users': users, 'usd_per_user_month': llm_per_user, 'activity_factor': activity}})
    genie_dbu_per_user = _f(rates, 'genie_dbu_per_user_month')
    genie_usd_per_dbu = _f(rates, 'usd_per_dbu_genie')
    genie_dbu = users * genie_dbu_per_user * activity
    on_behalf_of_user = bool((assumptions or {}).get('genie_on_behalf_of_user'))
    billable_dbu = genie_dbu
    if on_behalf_of_user:
        billable_dbu = max(0.0, genie_dbu - GENIE_FREE_DBU_PER_USER * users)
        notes.append('Genie priced with the 150 DBU/user/month free allowance, which requires calling Genie on behalf of the signed-in user — NOT how the app works today (it calls as the service principal, which gets no allowance).')
    lines.append({'key': 'genie', 'label': 'Genie (NL→SQL)', 'driver': 'users', 'usd': billable_dbu * genie_usd_per_dbu, 'formula': 'users × Genie DBU/user/month × (working days ÷ 22) × $/DBU' + (' − 150 DBU/user allowance' if on_behalf_of_user else ''), 'detail': {'genie_dbu_per_user_month': genie_dbu_per_user, 'activity_factor': activity, 'consumed_dbu': genie_dbu, 'billable_dbu': billable_dbu, 'usd_per_dbu': genie_usd_per_dbu}})
    capacity_specs = [{'key': 'sql', 'label': 'SQL warehouse (serverless)', 'per_unit': _f(a, 'queries_per_cluster'), 'multiplier': _f(a, 'queries_in_flight_per_user'), 'unit_usd_per_hr': _f(rates, 'dbu_per_hr_sql') * _f(rates, 'usd_per_dbu_sql'), 'unit_name': 'cluster', 'peak_hours': hours['sql_peak_hours'], 'in_hours_units': _f(a, 'min_clusters'), 'in_hours_hours': hours['sql_off_peak'], 'off_units': 0.0, 'formula': '(peak clusters × peak hours + baseline × remaining awake hours) × DBU per awake hour (observed, not the nameplate rate) × $/DBU'}, {'key': 'apps', 'label': 'Databricks Apps compute', 'per_unit': _f(a, 'users_per_instance'), 'multiplier': 1.0, 'unit_usd_per_hr': _f(rates, 'dbu_per_hr_apps') * _f(rates, 'usd_per_dbu_apps'), 'unit_name': 'instance', 'peak_hours': hours['peak_hours_month'], 'in_hours_units': _f(a, 'in_hours_instances'), 'in_hours_hours': hours['in_hours_offpeak'], 'off_units': _f(a, 'min_instances'), 'formula': '(peak instances × peak hours + in-hours floor × rest of the working day + off-hours instances × remaining hours) × DBU/hr × $/DBU'}, {'key': 'lakebase', 'label': 'Lakebase (Postgres) compute', 'per_unit': _f(a, 'users_per_cu'), 'multiplier': 1.0, 'unit_usd_per_hr': _f(rates, 'usd_per_cu_hr_lakebase'), 'unit_name': 'capacity unit', 'peak_hours': hours['peak_hours_month'], 'in_hours_units': _f(a, 'in_hours_cu'), 'in_hours_hours': hours['in_hours_offpeak'], 'off_units': _f(a, 'min_cu'), 'formula': '(peak CU × peak hours + in-hours floor × rest of the working day + off-hours CU × remaining hours) × $/CU-hr'}]
    breakpoints: list[dict] = []
    for spec in capacity_specs:
        rate = spec['unit_usd_per_hr']
        peak_units = _units(concurrent, spec['per_unit'], spec['multiplier'])
        peak_cost = peak_units * spec['peak_hours'] * rate
        in_hours_cost = spec['in_hours_units'] * spec['in_hours_hours'] * rate
        off_cost = spec['off_units'] * hours['off_hours_month'] * rate
        lines.append({'key': spec['key'], 'label': spec['label'], 'driver': 'concurrent', 'usd': peak_cost + in_hours_cost + off_cost, 'formula': spec['formula'], 'detail': {'peak_units': peak_units, 'unit_name': spec['unit_name'], 'peak_hours_month': spec['peak_hours'], 'in_hours_units': spec['in_hours_units'], 'in_hours_hours': spec['in_hours_hours'], 'off_units': spec['off_units'], 'off_hours': hours['off_hours_month'] if spec['off_units'] else 0.0, 'baseline_units': max(spec['in_hours_units'], spec['off_units']), 'baseline_hours': spec['in_hours_hours'] + (hours['off_hours_month'] if spec['off_units'] else 0.0), 'unit_usd_per_hr': rate, 'peak_usd': peak_cost, 'in_hours_usd': in_hours_cost, 'off_hours_usd': off_cost}})
        at = _next_step(concurrent, spec['per_unit'], spec['multiplier'])
        if at is not None:
            breakpoints.append({'key': spec['key'], 'label': spec['label'], 'unit_name': spec['unit_name'], 'at_concurrent_users': at, 'units_now': peak_units, 'units_after': peak_units + 1, 'usd_step': spec['peak_hours'] * rate})
    notes.append('Capacity lines assume autoscaling: peak sizing is held only during peak hours. Apps does not autoscale in the current configuration (min/max instances is not enabled), so this prices a setup that would need to be turned on.')

    def _pct(key: str | None) -> float:
        if not key:
            return 0.0
        return max(0.0, min(100.0, _f(rates, key, 0.0)))
    discounts = {'a': _pct('discount_a_pct'), 'b': _pct('discount_b_pct')}
    for line in lines:
        pct = _pct(LINE_DISCOUNT.get(line['key']))
        line['usd_list'] = line['usd']
        line['usd'] = line['usd'] * (1.0 - pct / 100.0)
        line['discount_pct'] = pct
    for bp in breakpoints:
        pct = _pct(LINE_DISCOUNT.get(bp['key']))
        bp['usd_step_list'] = bp['usd_step']
        bp['usd_step'] = bp['usd_step'] * (1.0 - pct / 100.0)
        bp['discount_pct'] = pct
    total_list = sum((line['usd_list'] for line in lines))
    total_net = sum((line['usd'] for line in lines))
    effective_pct = 100.0 * (1.0 - total_net / total_list) if total_list > 0 else 0.0
    return {'inputs': {'users': users, 'concurrent_users': concurrent}, 'discounts': discounts, 'effective_discount_pct': effective_pct, 'total_usd_month_list': total_list, 'assumptions': {k: _f(a, k) for k in DEFAULT_ASSUMPTIONS}, 'genie_on_behalf_of_user': on_behalf_of_user, 'hours': hours, 'lines': lines, 'total_usd_month': total_net, 'usd_per_user_month': total_net / users if users > 0 else None, 'breakpoints': sorted(breakpoints, key=lambda b: b['at_concurrent_users']), 'notes': notes}

def curve(users_max: float, points: int, concurrent: float, users: float, scale_concurrency: bool, assumptions: dict, rates: dict) -> list[dict]:
    points = max(2, min(int(points or 26), 200))
    users_max = max(1.0, float(users_max or 1))
    ratio = concurrent / users if users and users > 0 else 0.0
    step = users_max / (points - 1)
    out = []
    for i in range(points):
        u = round(i * step, 4)
        c = u * ratio if scale_concurrency else concurrent
        point = forecast(u, c, assumptions, rates)
        out.append({'users': u, 'concurrent_users': c, 'total_usd_month': point['total_usd_month'], 'usd_per_user_month': point['usd_per_user_month'], 'lines': {line['key']: line['usd'] for line in point['lines']}})
    return out
