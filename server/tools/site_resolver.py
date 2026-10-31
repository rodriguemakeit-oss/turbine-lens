"""Site-name resolution using accent folding and explicit ambiguity detection."""

from __future__ import annotations

import unicodedata

# A generic word matches a large slice of the fleet, so the candidate list has to be
# capped. It is surfaced as `truncated` rather than quietly shortened — a clipped list
# presented as the whole set is how a model ends up counting wrong.
MAX_CANDIDATES = 15

# Shortest word the loose pass will treat as evidence, on either side of the compare.
_MIN_TOKEN_LEN = 4

# Ranked strongest → weakest. The label is shown to the user, so it explains *why* a
# site is in the list.
_TIER_EXACT = (0, "exact")
_TIER_PREFIX = (1, "starts with")
_TIER_CONTAINS = (2, "contains")
_TIER_TOKEN = (3, "loose word match")


def fold(value: str | None) -> str:
    """Normalise a site name for comparison.

    Strips accents and collapses every non-alphanumeric run — apostrophes of any
    flavour included — to a single space. So `L'Épine Amanty`, `l'epine amanty` and
    `L’EPINE  AMANTY` all fold to `l epine amanty`, and `WIF_FREPN` folds to
    `wif frepn`.

    Apostrophes *separate* rather than vanish, which matters: it leaves `l` as its own
    word so `_tokens` can drop it as an article and let a query for "l'Epine" still
    reach `Epinette`. Deleting the apostrophe instead would glue it into `lepine`,
    match only `L'Epine Amanty`, and resolve silently — hiding the ambiguity this
    module exists to surface.
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    out = [ch if ch.isalnum() else " " for ch in stripped.lower()]
    return " ".join("".join(out).split())


# Words that carry no site identity, dropped before the loose pass. Two kinds:
# French articles, and the technology / structural words that appear in hundreds of
# farm names. Leaving the latter in made "Red Sea Wind Energy" loose-match six
# unrelated farms on `wind` and `energy` alone — noise that then hid the real farm.
# Only the loose pass consults this; exact / prefix / contains still see the whole
# string, so a site actually named "Wind Farm" is still matchable.
_STOPWORDS = {
    # articles and particles
    "l", "le", "la", "les", "de", "du", "des", "d", "the", "el", "las", "los",
    # technology and structure
    "wind", "solar", "bess", "hybrid", "farm", "farms", "park", "parc", "energy",
    "energie", "energia", "phase", "extension", "project", "sas", "llc", "sarl",
}


def _tokens(folded: str) -> list[str]:
    """Words of a folded string, minus the ones that carry no identity.

    `l'Epine` folds to `l epine`; the `l` is an article, not a name, so a query for it
    must still reach `Epinette`. Dropping it is what keeps the ambiguity visible.
    """
    return [t for t in folded.split() if t not in _STOPWORDS]


def _tier(query_folded: str, query_tokens: list[str], farm: str, code: str) -> tuple[int, str] | None:
    """Strongest tier at which `query` matches this farm, or None if it does not."""
    farm_folded = fold(farm)
    code_folded = fold(code)

    # Compare with word boundaries removed as well as folded, so that whether the user
    # typed the apostrophe or not stops mattering: `l'Epine`, `l Epine` and `lepine`
    # all squash to `lepine` and all reach both `L'Epine Amanty` and `L'épinette`.
    # This subsumes the spaced comparison (a spaced prefix is also a squashed one), so
    # only the squashed form is tested here; the token pass below still works on words,
    # which is where dropping the article matters.
    q_squashed = query_folded.replace(" ", "")
    farm_squashed = farm_folded.replace(" ", "")
    code_squashed = code_folded.replace(" ", "")

    if q_squashed and q_squashed in (farm_squashed, code_squashed):
        return _TIER_EXACT
    if q_squashed and (farm_squashed.startswith(q_squashed) or code_squashed.startswith(q_squashed)):
        return _TIER_PREFIX
    if q_squashed and (q_squashed in farm_squashed or q_squashed in code_squashed):
        return _TIER_CONTAINS

    # Loose pass: any significant word of the query appearing inside a word of the
    # farm name. This is what surfaces `Epinette - Hucqueliers` for "l'Epine" — a real
    # confusion the user should get to rule out rather than one we resolve for them.
    # Both sides need the length floor, not just the query side. Without it a
    # one-letter word in the data matches almost any query — the `h` of `Triple H`
    # sits inside "nowhere", which made a nonsense name resolve to a real 92-turbine
    # farm, and the `i` of `San Pedro I` put that farm in the list for "l'Epine".
    farm_words = [w for w in farm_folded.split() + code_folded.split() if len(w) >= _MIN_TOKEN_LEN]
    for token in query_tokens:
        if len(token) < _MIN_TOKEN_LEN:
            continue
        for word in farm_words:
            if token in word or word in token:
                return _TIER_TOKEN
    return None


def find_candidates(query: str, farms: list[dict]) -> dict:
    """Rank `farms` against `query`.

    `farms` is the farm dimension as rows of {farm, farm_code, turbines, manufacturer,
    model}. Returns {status, candidates, total, truncated} where status is one of:

      exact      — one folded full-string hit on `farm` or `farm_code`; safe to use
      unique     — exactly one candidate overall; safe to use, but say which it is
      ambiguous  — two or more plausible sites; must go back to the user
      none       — nothing matched

    Ordering is fully deterministic (tier, then folded name, then code) so that a later
    call with the same `query` reproduces the same list and a `choice` index stays
    meaningful without the server holding any state between calls.
    """
    q_folded = fold(query)
    q_tokens = _tokens(q_folded)
    if not q_folded:
        return {"status": "none", "candidates": [], "total": 0, "truncated": False}

    scored: list[tuple[int, str, str, dict]] = []
    for row in farms:
        farm = str(row.get("farm") or "")
        code = str(row.get("farm_code") or "")
        tier = _tier(q_folded, q_tokens, farm, code)
        if tier is None:
            continue
        rank, why = tier
        scored.append((rank, fold(farm), code, {**row, "match": why}))

    scored.sort(key=lambda s: (s[0], s[1], s[2]))
    candidates = [s[3] for s in scored]

    # A single full-string hit settles it — a weaker match elsewhere in the fleet is
    # not evidence of ambiguity when the user typed the name (or the code) exactly.
    # Anything short of that and two survivors are enough to go back to the user:
    # letting the strongest tier win outright would re-create the original bug, where
    # one `starts with` hit beat one `loose` hit and "l'Epine" resolved silently.
    exact = [c for c in candidates if c["match"] == "exact"]
    if len(exact) == 1:
        status, candidates = "exact", exact
    elif len(candidates) == 1:
        status = "unique"
    elif not candidates:
        status = "none"
    else:
        status = "ambiguous"

    total = len(candidates)
    truncated = total > MAX_CANDIDATES
    return {
        "status": status,
        "candidates": candidates[:MAX_CANDIDATES],
        "total": total,
        "truncated": truncated,
    }


# --- Two-tier lookup: the wind registry first, the fleet hierarchy as fallback ----
#
# `pic.wind_turbine` is the reference for wind farms and turbines and is always
# consulted first. `pic.hierarchy` is the more general level — every technology
# (wind, solar, BESS, hybrid) and every farm, including the 137 wind farms that have
# no turbines recorded — and is consulted only when the wind registry has nothing.
#
# A fallback, not a union: a name that hits the wind registry never shows its
# hierarchy siblings as candidates. That hides a real alternative for the two names
# fleet-wide that exist as both an active wind farm and a farm of another technology
# (`Bollène` is a wind farm AND a solar farm), so those are reported as a note
# instead — visible, without changing which tier decided.
WIND_REGISTRY = "wind_turbine"
FLEET_HIERARCHY = "hierarchy"


def resolve(query: str, wind_rows: list[dict], hierarchy_rows: list[dict]) -> dict:
    """Match `query` against the wind registry, falling back to the fleet hierarchy.

    Returns the `find_candidates` shape plus:
      tier        — which table decided (`wind_turbine` or `hierarchy`)
      candidates  — each annotated with `source` and `active`
      same_name_other_technologies — for a wind-registry hit, farms of another
                    technology sharing the name that the fallback rule cannot show

    **`active` is inferred from presence in `pic.wind_turbine`**, per the agreed rule:
    a farm the wind registry knows is reported active, one only the hierarchy knows is
    reported not active. Two known inaccuracies ride along, so callers must pass the
    caveats on rather than present `active` as measured:
      * the registry holds 25 DECOMMISSIONED farms, which this rule calls active —
        `registry_status` carries the real value so the caller can say so;
      * no non-wind registry exists, so every solar / BESS / hybrid farm falls to the
        hierarchy and is reported not active whatever its true state.
    """
    wind_codes = {str(r.get("farm_code")) for r in wind_rows}

    # "wind registry first" means first refusal on a REAL match, not on any match at
    # all. A loose word hit is a guess, and letting one stop the search meant
    # `WIF_EGRED` — the Red Sea farm's own code — resolved to `Negredo`, because
    # `egred` sits inside `n-egred-o` and the fallback never ran. So tier 1 answers
    # only when it has something better than a guess; ties still go to tier 1.
    wind = find_candidates(query, wind_rows)
    if _has_strong_match(wind):
        found, tier = wind, WIND_REGISTRY
    else:
        hierarchy = find_candidates(query, hierarchy_rows)
        if _has_strong_match(hierarchy) or not wind["candidates"]:
            found, tier = hierarchy, FLEET_HIERARCHY
        else:
            found, tier = wind, WIND_REGISTRY

    for candidate in found["candidates"]:
        candidate["source"] = tier
        # Membership decides, not the tier: a hierarchy row can carry a different name
        # for a farm_code the registry does know, and that farm is still active.
        candidate["active"] = str(candidate.get("farm_code")) in wind_codes

    found["tier"] = tier
    found["same_name_other_technologies"] = (
        _same_name_elsewhere(found["candidates"], hierarchy_rows) if tier == WIND_REGISTRY else []
    )
    return found


def _has_strong_match(found: dict) -> bool:
    """True if any candidate matched on more than a loose word — i.e. not just a guess."""
    return any(c["match"] != _TIER_TOKEN[1] for c in found["candidates"])


def _same_name_elsewhere(candidates: list[dict], hierarchy_rows: list[dict]) -> list[dict]:
    """Farms sharing a candidate's name under a different code — what the fallback hides."""
    wanted = {fold(c.get("farm")): str(c.get("farm_code")) for c in candidates}
    out = []
    for row in hierarchy_rows:
        name = fold(row.get("farm"))
        if name in wanted and str(row.get("farm_code")) != wanted[name]:
            out.append(
                {
                    "farm": row.get("farm"),
                    "technology": row.get("farm_technology") or row.get("technology"),
                    "note": "same name, different site — not offered because the wind registry matched first",
                }
            )
    return out


def label(candidate: dict) -> str:
    """One human-readable line for a candidate — no farm_code.

    The code is the value the Genie spaces and `wind_stops` key on, so it is
    deliberately absent from anything shown while the site is still ambiguous: the
    agent cannot query a site it has no identifier for.

    Whether the farm is active leads the detail, because that is what the user has to
    be told about a site before anything else is read off it.
    """
    bits = [str(candidate.get("farm") or "?")]
    tail: list[str] = []

    if "active" in candidate:
        tail.append("active" if candidate["active"] else "not active")
    # The registry's own word, shown only when it contradicts the inferred `active`.
    registry_status = str(candidate.get("registry_status") or "")
    if registry_status and registry_status != "IN OPERATION":
        tail.append(f"registry says {registry_status}")

    if candidate.get("source") == FLEET_HIERARCHY:
        technology = candidate.get("farm_technology") or candidate.get("technology")
        if technology:
            tail.append(str(technology))
        if candidate.get("business_unit"):
            tail.append(str(candidate["business_unit"]))
        substations = candidate.get("substations")
        if substations:
            tail.append(f"{substations} substation{'s' if int(substations) != 1 else ''}")
    else:
        if candidate.get("turbines"):
            tail.append(f"{candidate['turbines']} turbines")
        hardware = " ".join(
            str(candidate.get(k)) for k in ("manufacturer", "model") if candidate.get(k)
        )
        if hardware:
            tail.append(hardware)

    if tail:
        bits.append(" · ".join(tail))
    return " — ".join(bits)
