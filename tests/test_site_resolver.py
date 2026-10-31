"""Matching regression tests using synthetic site names and codes."""

from __future__ import annotations

import pytest

from server.tools import site_resolver as sr

FARMS = [
    {"farm": "L'Aube Plateau", "farm_code": "DEMO_AUBE", "turbines": 7,
     "manufacturer": "Senvion", "model": "MM92"},
    {"farm": "L'aubette", "farm_code": "DEMO_AUBT", "turbines": 6,
     "manufacturer": "Nordex", "model": "N117"},
    {"farm": "Aubette - Vallon", "farm_code": "DEMO_AUVL", "turbines": 6,
     "manufacturer": "Enercon", "model": "E-82"},
    # One-letter words: the regression guards.
    {"farm": "Ridge Q", "farm_code": "DEMO_RDGQ", "turbines": 92,
     "manufacturer": "General Electric", "model": "GE-X-116"},
    {"farm": "Monte Verde Z", "farm_code": "DEMO_MTVZ", "turbines": 18,
     "manufacturer": "Gamesa", "model": "G90"},
    {"farm": "Clairval", "farm_code": "DEMO_CLRV", "turbines": 4,
     "manufacturer": "Gamesa", "model": "G52"},
]


def status(query: str) -> str:
    return sr.find_candidates(query, FARMS)["status"]


def farms_for(query: str) -> list[str]:
    return [c["farm"] for c in sr.find_candidates(query, FARMS)["candidates"]]


# --- the reported bug: "l'Aube" must not silently resolve --------------------

def test_ambiguous_site_is_not_resolved_silently():
    """The case users complained about: two plausible sites, so no auto-pick."""
    assert status("l'Aube") == "ambiguous"
    assert "L'Aube Plateau" in farms_for("l'Aube")
    assert "Aubette - Vallon" in farms_for("l'Aube")


@pytest.mark.parametrize("typed", ["l'Aube", "l’Aube", "l aube", "laube", "L'AUBE"])
def test_apostrophe_style_does_not_change_the_verdict(typed):
    """However the apostrophe is typed (or omitted), the ambiguity is the same."""
    assert status(typed) == "ambiguous"
    assert "L'Aube Plateau" in farms_for(typed)


# --- unambiguous input must resolve, so the check does not become noise -------

@pytest.mark.parametrize(
    "typed",
    ["L'Aube Plateau", "L’Aube Plateau", "l'aube plateau", "DEMO_AUBE", "demo_aube", "demoaube"],
)
def test_full_name_or_code_resolves(typed):
    res = sr.find_candidates(typed, FARMS)
    assert res["status"] == "exact"
    assert [c["farm"] for c in res["candidates"]] == ["L'Aube Plateau"]


def test_exact_match_wins_over_weaker_matches_elsewhere():
    """`Clairval` is also a loose hit on nothing else, but the point is it settles."""
    assert status("Clairval") == "exact"


# --- the loose pass must not match on one-letter words ------------------------

@pytest.mark.parametrize("nonsense", ["Zzz Nowhere", "qqqq", "'''", ""])
def test_nonsense_finds_nothing(nonsense):
    """`Ridge Q` once matched "nowhere" through its lone `h`."""
    assert status(nonsense) == "none"


def test_one_letter_farm_words_do_not_pollute_candidates():
    assert "Ridge Q" not in farms_for("l'Aube")
    assert "Monte Verde Z" not in farms_for("l'Aube")


# --- ordering and truncation -------------------------------------------------

def test_ordering_is_deterministic_so_choice_indexes_are_stable():
    """`choice` is re-derived from the name alone, so the order must not drift."""
    first = farms_for("l'Aube")
    for _ in range(5):
        assert farms_for("l'Aube") == first


def test_strongest_matches_come_first():
    ranks = [c["match"] for c in sr.find_candidates("l'Aube", FARMS)["candidates"]]
    assert ranks == sorted(ranks, key=["exact", "starts with", "contains", "loose word match"].index)


def test_truncation_is_reported_not_hidden():
    many = [
        {"farm": f"Saint Test {i:02d}", "farm_code": f"WIF_FRS{i:02d}", "turbines": 3,
         "manufacturer": "Vestas", "model": "V90"}
        for i in range(sr.MAX_CANDIDATES + 5)
    ]
    res = sr.find_candidates("Saint", many)
    assert res["status"] == "ambiguous"
    assert res["truncated"] is True
    assert len(res["candidates"]) == sr.MAX_CANDIDATES
    assert res["total"] == sr.MAX_CANDIDATES + 5


# --- labels must not carry the identifier ------------------------------------

def test_label_withholds_the_farm_code():
    """An ambiguous result must give the user enough to choose and the agent nothing
    to query with — the code is what Genie and wind_stop_loss key on."""
    for candidate in sr.find_candidates("l'Aube", FARMS)["candidates"]:
        text = sr.label(candidate)
        assert candidate["farm"] in text
        assert candidate["farm_code"] not in text


def test_label_reads_as_a_choice():
    label = sr.label(FARMS[0])
    assert label == "L'Aube Plateau — 7 turbines · Senvion MM92"


# --- two-tier lookup: wind registry first, fleet hierarchy as fallback -------
#
# HIERARCHY contains every WIND row above (the measured containment) plus farms the
# registry has no turbines for, across every technology.

HIERARCHY = [
    {"farm": "L'Aube Plateau", "farm_code": "DEMO_AUBE", "farm_technology": "WIND",
     "technology": "WIND", "business_unit": "R&B EUROPE", "substations": 2},
    {"farm": "L'aubette", "farm_code": "DEMO_AUBT", "farm_technology": "WIND",
     "technology": "WIND", "business_unit": "R&B EUROPE", "substations": 1},
    {"farm": "Aubette - Vallon", "farm_code": "DEMO_AUVL", "farm_technology": "WIND",
     "technology": "WIND", "business_unit": "R&B EUROPE", "substations": 1},
    {"farm": "Ridge Q", "farm_code": "DEMO_RDGQ", "farm_technology": "WIND",
     "technology": "WIND", "business_unit": "R&B NORAM", "substations": 3},
    {"farm": "Monte Verde Z", "farm_code": "DEMO_MTVZ", "farm_technology": "WIND",
     "technology": "WIND", "business_unit": "R&B IBERIA", "substations": 1},
    {"farm": "Clairval", "farm_code": "DEMO_CLRV", "farm_technology": "WIND",
     "technology": "WIND", "business_unit": "R&B EUROPE", "substations": 1},
    # Wind farm with no turbines recorded — one of the 137 the fallback exists for.
    {"farm": "Aquettes", "farm_code": "WIF_FRAQU", "farm_technology": "WIND",
     "technology": "WIND", "business_unit": "R&B EUROPE", "substations": 2},
    # Non-wind, so no registry can ever confirm it.
    {"farm": "Le Bousquet d'Orb", "farm_code": "SOF_FRBOS", "farm_technology": "SOLAR",
     "technology": "SOLAR", "business_unit": "AKUO Energy 1", "substations": 1},
    # The name collision: an active wind farm and a solar farm.
    {"farm": "Bollène", "farm_code": "WIF_FRBOL", "farm_technology": "WIND",
     "technology": "WIND", "business_unit": "R&B EUROPE", "substations": 1},
    {"farm": "Bollène", "farm_code": "SOF_FRBLN", "farm_technology": "SOLAR",
     "technology": "SOLAR", "business_unit": "R&B EUROPE", "substations": 3},
]

WIND_PLUS_BOLLENE = FARMS + [
    {"farm": "Bollène", "farm_code": "WIF_FRBOL", "turbines": 3,
     "manufacturer": "Vestas", "model": "V90", "registry_status": "IN OPERATION"},
]


def test_wind_registry_is_checked_first():
    res = sr.resolve("l'Aube", WIND_PLUS_BOLLENE, HIERARCHY)
    assert res["tier"] == sr.WIND_REGISTRY
    assert all(c["source"] == sr.WIND_REGISTRY for c in res["candidates"])
    assert all(c["active"] for c in res["candidates"])


def test_hierarchy_is_used_only_when_the_registry_misses():
    """`Aquettes` is a wind farm with no turbines recorded — registry miss, hierarchy hit."""
    res = sr.resolve("Aquettes", WIND_PLUS_BOLLENE, HIERARCHY)
    assert res["tier"] == sr.FLEET_HIERARCHY
    assert res["status"] == "exact"
    only = res["candidates"][0]
    assert only["farm_code"] == "WIF_FRAQU"
    assert only["active"] is False


def test_non_wind_farm_resolves_from_the_hierarchy_as_not_active():
    res = sr.resolve("Le Bousquet d'Orb", WIND_PLUS_BOLLENE, HIERARCHY)
    assert res["tier"] == sr.FLEET_HIERARCHY
    assert res["candidates"][0]["active"] is False
    assert res["candidates"][0]["farm_technology"] == "SOLAR"


def test_active_is_decided_by_registry_membership_not_by_tier():
    """A hierarchy row for a farm the registry DOES know must still read as active.

    Guards the case where the two tables spell the same farm_code's name differently:
    the tier that answered is not what makes a farm active, membership is.
    """
    # The name matches nothing in the registry, so the hierarchy answers — but the
    # farm_code it carries is one the registry knows, so the farm is active.
    hierarchy_only_name = [
        {"farm": "Zephyr Plateau", "farm_code": "DEMO_AUBE", "farm_technology": "WIND",
         "technology": "WIND", "business_unit": "R&B EUROPE", "substations": 2},
    ]
    res = sr.resolve("Zephyr Plateau", WIND_PLUS_BOLLENE, hierarchy_only_name)
    assert res["tier"] == sr.FLEET_HIERARCHY
    assert res["candidates"][0]["farm_code"] == "DEMO_AUBE"
    assert res["candidates"][0]["active"] is True


def test_fallback_never_becomes_a_union():
    """A registry hit must not pull in hierarchy-only siblings as candidates."""
    res = sr.resolve("Bollène", WIND_PLUS_BOLLENE, HIERARCHY)
    assert res["tier"] == sr.WIND_REGISTRY
    assert [c["farm_code"] for c in res["candidates"]] == ["WIF_FRBOL"]


def test_the_hidden_sibling_is_reported_as_a_note():
    """What the fallback rule hides has to be visible somewhere."""
    res = sr.resolve("Bollène", WIND_PLUS_BOLLENE, HIERARCHY)
    others = res["same_name_other_technologies"]
    assert [o["technology"] for o in others] == ["SOLAR"]


def test_no_hidden_sibling_note_when_the_hierarchy_answered():
    res = sr.resolve("Le Bousquet d'Orb", WIND_PLUS_BOLLENE, HIERARCHY)
    assert res["same_name_other_technologies"] == []


def test_labels_state_whether_the_farm_is_active():
    wind = sr.resolve("l'Aube", WIND_PLUS_BOLLENE, HIERARCHY)["candidates"][0]
    assert sr.label(wind) == "L'Aube Plateau — active · 7 turbines · Senvion MM92"

    hier = sr.resolve("Aquettes", WIND_PLUS_BOLLENE, HIERARCHY)["candidates"][0]
    assert sr.label(hier) == "Aquettes — not active · WIND · R&B EUROPE · 2 substations"


def test_decommissioned_farm_is_flagged_in_the_label():
    """The presence rule calls it active; the registry's own word must still show."""
    registry = [
        {"farm": "Old Ridge", "farm_code": "WIF_FROLD", "turbines": 4,
         "manufacturer": "Vestas", "model": "V52", "registry_status": "DECOMMISSIONED"},
    ]
    candidate = sr.resolve("Old Ridge", registry, HIERARCHY)["candidates"][0]
    assert candidate["active"] is True  # per the agreed presence rule
    assert "registry says DECOMMISSIONED" in sr.label(candidate)


def test_both_tiers_missing_is_still_none():
    res = sr.resolve("Zzz Nowhere", WIND_PLUS_BOLLENE, HIERARCHY)
    assert res["status"] == "none"
    assert res["candidates"] == []


# --- the Red Sea regressions -------------------------------------------------
#
# `RED SEA WIND ENERGY (PHASE 3)` is a real hierarchy-only wind farm whose code is
# WIF_EGRED. It exposed two bugs at once: generic words in a query loose-matched
# unrelated farms in the registry, and those loose hits then blocked the fallback.

RED_SEA_HIERARCHY = HIERARCHY + [
    {"farm": "RED SEA WIND ENERGY (PHASE 3)", "farm_code": "WIF_EGRED",
     "farm_technology": "WIND", "technology": "WIND", "business_unit": "R&B AMEA",
     "substations": 1},
]
REGISTRY_WITH_NOISE = WIND_PLUS_BOLLENE + [
    # Real farms whose names contain the generic words a query might also contain.
    {"farm": "Kukinia Wind Farm", "farm_code": "WIF_PLKUK", "turbines": 23,
     "manufacturer": "Enercon", "model": "E-82", "registry_status": "IN OPERATION"},
    {"farm": "Ras Ghareb Wind Energy", "farm_code": "WIF_EGRGB", "turbines": 125,
     "manufacturer": "Gamesa", "model": "G97", "registry_status": "IN OPERATION"},
    # `egred` is a substring of `n-egred-o`: the trap that stole WIF_EGRED.
    {"farm": "Negredo", "farm_code": "WIF_ESNEG", "turbines": 9,
     "manufacturer": "Vestas", "model": "V90", "registry_status": "IN OPERATION"},
]


@pytest.mark.parametrize(
    "typed",
    ["Red Sea Wind Energy", "RED SEA WIND ENERGY (PHASE 3)", "red sea wind", "Red Sea", "WIF_EGRED"],
)
def test_red_sea_resolves_however_it_is_typed(typed):
    res = sr.resolve(typed, REGISTRY_WITH_NOISE, RED_SEA_HIERARCHY)
    assert res["tier"] == sr.FLEET_HIERARCHY
    assert res["candidates"][0]["farm_code"] == "WIF_EGRED"
    assert res["candidates"][0]["active"] is False


def test_generic_words_do_not_create_loose_matches():
    """`wind` and `energy` are in hundreds of names and identify nothing.

    Leaving them in made "Red Sea Wind Energy" match six unrelated farms.
    """
    farms = [c["farm"] for c in sr.find_candidates("Red Sea Wind Energy", REGISTRY_WITH_NOISE)["candidates"]]
    assert "Kukinia Wind Farm" not in farms
    assert "Ras Ghareb Wind Energy" not in farms


def test_a_farm_named_from_generic_words_still_resolves():
    """The stopwords must not make such a farm unmatchable — exact still sees it all."""
    res = sr.resolve("Ras Ghareb Wind Energy", REGISTRY_WITH_NOISE, RED_SEA_HIERARCHY)
    assert res["tier"] == sr.WIND_REGISTRY
    assert res["candidates"][0]["farm_code"] == "WIF_EGRGB"


def test_a_loose_registry_hit_does_not_block_the_fallback():
    """The WIF_EGRED -> Negredo bug: exact code in, wrong farm out, no question asked."""
    registry_loose_only = sr.find_candidates("WIF_EGRED", REGISTRY_WITH_NOISE)
    assert [c["farm"] for c in registry_loose_only["candidates"]] == ["Negredo"]
    assert registry_loose_only["candidates"][0]["match"] == "loose word match"

    res = sr.resolve("WIF_EGRED", REGISTRY_WITH_NOISE, RED_SEA_HIERARCHY)
    assert res["tier"] == sr.FLEET_HIERARCHY
    assert res["candidates"][0]["farm"] == "RED SEA WIND ENERGY (PHASE 3)"


def test_a_strong_registry_hit_still_wins_over_the_hierarchy():
    """The fix must not weaken "wind registry first" for a real match."""
    res = sr.resolve("l'Aube", REGISTRY_WITH_NOISE, RED_SEA_HIERARCHY)
    assert res["tier"] == sr.WIND_REGISTRY


def test_when_both_tiers_are_only_guesses_the_registry_still_answers():
    """Ties go to tier 1 — `pic.wind_turbine` keeps first refusal."""
    registry = [{"farm": "Negredo", "farm_code": "WIF_ESNEG", "turbines": 9,
                 "manufacturer": "Vestas", "model": "V90", "registry_status": "IN OPERATION"}]
    hierarchy = [{"farm": "Negredillo", "farm_code": "SOF_ESNGD", "farm_technology": "SOLAR",
                  "technology": "SOLAR", "business_unit": "R&B IBERIA", "substations": 1}]
    res = sr.resolve("egredo", registry, hierarchy)
    assert res["tier"] == sr.WIND_REGISTRY
