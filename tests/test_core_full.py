"""Full core contract, boundary and independently computed invariant checks."""

from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path
import random
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from meal_clear.core import (
    InputError,
    amend_request,
    break_even,
    check_person,
    compare_results,
    compose,
    fingerprint,
    money_to_cents,
    quote_signature,
    validate_catalog,
    validate_request,
)


class CoreFullTests(unittest.TestCase):
    """Cover each declared core input, output state and arithmetic invariant."""

    def setUp(self) -> None:
        """Load the explicitly fictional reference data without mutating it."""
        self.req = json.loads((ROOT / "examples/request.json").read_text())
        self.cat = json.loads((ROOT / "examples/demo-catalog.json").read_text())

    def test_C01_money_invalid_and_large_exact_values(self) -> None:
        """Invalid units/precision/nonfinite values fail; large cents remain exact."""
        self.assertEqual(money_to_cents("90071992547409.91", "CNY"), 9007199254740991)
        for value in ["bad", None, {}, "Infinity", "-Infinity", "-0.001"]:
            with self.subTest(value=value), self.assertRaises(InputError):
                money_to_cents(value, "CNY")
        self.assertEqual(money_to_cents("0.00", "CNY"), 0)

    def test_C02_request_top_level_and_context_contract(self) -> None:
        """Missing, unknown and wrongly typed context fields are rejected."""
        requests = [
            None,
            [],
            "bad",
            {**self.req, "extra": 1},
            {**self.req, "context": None},
        ]
        for change in [
            {},
            {"store_code": "DEMO"},
            {**self.req["context"], "store_code": " "},
            {**self.req["context"], "channel": "delivery"},
            {**self.req["context"], "meal_time": 1},
            {**self.req["context"], "extra": "x"},
        ]:
            requests.append({**self.req, "context": change})
        for request in requests:
            with self.subTest(request=request), self.assertRaises(InputError):
                validate_request(request)

    def test_C03_people_and_atomic_values_contract(self) -> None:
        """Reject invalid person counts, IDs, field containers and atom types."""
        variants = [
            [],
            None,
            [{}, {}, {}],
            [None],
            [{"id": "A", "extra": 1}],
            [{"id": ""}],
            [{"id": " "}],
            [{"id": "A"}, {"id": "A"}],
            [{"id": "A", "hard": []}],
            [{"id": "A", "hard": {str(i): True for i in range(13)}}],
            [{"id": "A", "hard": {"x": []}}],
            [{"id": "A", "prefer": {"x": 1.5}}],
            [{"id": "A", "hard": {1: True}}],
        ]
        for people in variants:
            with self.subTest(people=people), self.assertRaises(InputError):
                validate_request({**self.req, "people": people})
        req = validate_request({**self.req, "people": [{"id": "A"}]})
        self.assertEqual(req["people"][0], {"id": "A", "hard": {}, "prefer": {}})

    def test_C04_patch_merge_delete_and_invalid_shapes(self) -> None:
        """Deletion is explicit; unchanged people, context and source request survive."""
        original = copy.deepcopy(self.req)
        changed = amend_request(
            self.req,
            {
                "people": {
                    "B": {
                        "hard": {"non_spicy": None, "main_meat": "beef"},
                        "prefer": {"main_meat": None},
                    }
                },
                "budget_cents": 5100,
                "context": {"store_code": "NEW"},
            },
        )
        self.assertEqual(changed["people"][0], original["people"][0])
        self.assertNotIn("non_spicy", changed["people"][1]["hard"])
        self.assertEqual(changed["people"][1]["prefer"], {})
        self.assertEqual(changed["context"]["store_code"], "NEW")
        self.assertEqual(changed["budget_cents"], 5100)
        self.assertEqual(self.req, original)
        for patch in [
            None,
            [],
            {"x": 1},
            {"context": []},
            {"people": []},
            {"people": {"B": None}},
            {"people": {"B": {"hard": []}}},
        ]:
            with self.subTest(patch=patch), self.assertRaises(InputError):
                amend_request(self.req, patch)

    def test_C05_catalog_contract_rejects_malformed_values(self) -> None:
        """Malformed metadata, missing meal roles and price/quantity errors fail safely."""
        variants = [
            None,
            [],
            {**self.cat, "mode": []},
            {**self.cat, "mode": "pretend"},
            {**self.cat, "as_of": None},
            {**self.cat, "offers": None},
        ]
        for mutate in [
            "duplicate",
            "empty_id",
            "empty_name",
            "empty_items",
            "no_drink",
            "fact_source",
            "fact_value",
            "negative_price",
            "bool_card",
            "blank_code",
            "zero_qty",
            "mode_bool",
        ]:
            cat = copy.deepcopy(self.cat)
            o = cat["offers"][0]
            if mutate == "duplicate":
                cat["offers"].append(copy.deepcopy(o))
            elif mutate == "empty_id":
                o["id"] = ""
            elif mutate == "empty_name":
                o["name"] = ""
            elif mutate == "empty_items":
                o["items"] = []
            elif mutate == "no_drink":
                o["items"] = o["items"][:1]
            elif mutate == "fact_source":
                o["facts"]["non_spicy"]["source"] = ""
            elif mutate == "fact_value":
                o["facts"]["non_spicy"].pop("value")
            elif mutate == "negative_price":
                o["price_cents"] = -1
            elif mutate == "bool_card":
                o["card_fee_cents"] = True
            elif mutate == "blank_code":
                o["items"][0]["code"] = ""
            elif mutate == "zero_qty":
                o["items"][0]["quantity"] = 0
            else:
                cat["mode"] = False
            variants.append(cat)
        for cat in variants:
            with self.subTest(catalog=cat), self.assertRaises(InputError):
                validate_catalog(cat)

    def test_C06_three_state_and_strict_fact_type(self) -> None:
        """Absent/null evidence is unknown; false and conflicting values stay distinct."""
        person = {"hard": {"x": False}}
        for fact, status in [
            (None, "unknown"),
            ({"value": None, "source": "x"}, "unknown"),
            ({"value": False, "source": "x"}, "verified"),
            ({"value": True, "source": "x"}, "violated"),
            ({"value": 0, "source": "x"}, "violated"),
        ]:
            offer = {"facts": {} if fact is None else {"x": fact}}
            with self.subTest(fact=fact):
                self.assertEqual(check_person(person, offer)[0]["status"], status)

    def test_C07_live_quotes_bind_context_signature_and_verification(self) -> None:
        """Local prices cannot become live quotes; invalid/missing evidence is not ready."""
        self.cat["mode"] = "live"
        first = compose(self.req, self.cat)
        plan = first["plans"][0]
        sig = plan["signature"]
        good = {
            "signature": sig,
            "context": self.req["context"],
            "verified": True,
            "total_cents": 3800,
            "source": "模拟官方",
            "quoted_at": "2026-10-19T00:00:00+00:00",
        }
        after = compose(self.req, self.cat, {sig: good})
        self.assertEqual(after["status"], "ready")
        self.assertEqual(after["recommendations"][0]["total_cents"], 3800)
        for change in [
            {"signature": "bad"},
            {"context": {}},
            {"verified": False},
            {"total_cents": True},
            {"total_cents": -1},
            {"total_cents": None},
        ]:
            with self.subTest(change=change):
                self.assertFalse(
                    compose(self.req, self.cat, {sig: {**good, **change}}).get(
                        "recommendations"
                    )
                )

    def test_C08_unknown_price_and_fee_never_assume_zero(self) -> None:
        """Missing price, missing fees and bool fees cannot produce a final amount."""
        for change in [None, True, -1]:
            cat = copy.deepcopy(self.cat)
            cat["order_fee_cents"] = change
            with self.subTest(fee=change):
                self.assertEqual(
                    compose(self.req, cat)["status"], "needs_price_verification"
                )
        cat = copy.deepcopy(self.cat)
        for o in cat["offers"]:
            o["price_cents"] = None
        self.assertEqual(compose(self.req, cat)["status"], "needs_price_verification")

    def test_C09_search_limits_and_no_input_mutation(self) -> None:
        """At most 30 candidates per person and 900 pairs, with zero input mutation."""
        cat = copy.deepcopy(self.cat)
        base = cat["offers"][0]
        cat["offers"] = []
        for i in range(45):
            item = copy.deepcopy(base)
            item.update(id=f"offer-{i}", price_cents=1000 + i)
            cat["offers"].append(item)
        snapshot = copy.deepcopy(cat)
        out = compose(self.req, cat)
        self.assertEqual(out["search"]["checked_combinations"], 900)
        self.assertLessEqual(len(out["recommendations"]), 2)
        self.assertEqual(cat, snapshot)

    def test_C10_difference_missing_price_and_unchanged_plan(self) -> None:
        """No false delta on failure; unchanged chosen meal reports a zero price delta."""
        before = compose(self.req, self.cat)
        self.assertEqual(
            compare_results(before, copy.deepcopy(before))["delta_cents"], 0
        )
        self.assertEqual(compare_results(before, copy.deepcopy(before))["changes"], [])
        failed = compose({**self.req, "budget_cents": 1}, self.cat)
        self.assertIsNone(compare_results(before, failed)["delta_cents"])
        changed = copy.deepcopy(before)
        changed["recommendations"][0]["total_cents"] = None
        self.assertIsNone(compare_results(before, changed)["delta_cents"])

    def test_C11_card_rounding_and_invalid_amount_types(self) -> None:
        """Zero fee is immediate break even; invalid fees/savings must not be coerced."""
        self.assertEqual(break_even(0, 0), 0)
        for fee, saving in [(-1, 100), (True, 100), (100, True), (100, 1.0)]:
            with self.subTest(fee=fee, saving=saving), self.assertRaises(InputError):
                break_even(fee, saving)

    def test_C12_signatures_track_context_items_and_card_fee(self) -> None:
        """Identity is stable by field order but changes with transaction-affecting input."""
        offer = self.cat["offers"][0]
        baseline = quote_signature(self.req["context"], [offer])
        for change in [
            {"card_fee_cents": 1},
            {"items": [{"code": "changed"}]},
            {"wire_items": [{"code": "X"}]},
        ]:
            with self.subTest(change=change):
                self.assertNotEqual(
                    quote_signature(self.req["context"], [{**offer, **change}]),
                    baseline,
                )
        self.assertNotEqual(
            fingerprint(self.req),
            fingerprint(amend_request(self.req, {"context": {"meal_time": "other"}})),
        )

    def test_C13_seeded_menus_against_independent_exhaustive_oracle(self) -> None:
        """For 120 small menus, every feasible outcome matches an independent enumerator."""
        rng = random.Random(20261019)
        for index in range(120):
            req = copy.deepcopy(self.req)
            req["budget_cents"] = rng.randint(1, 12000)
            req["people"] = req["people"][: rng.choice((1, 2))]
            cat = copy.deepcopy(self.cat)
            cat["offers"] = []
            for number in range(rng.randint(1, 7)):
                offer = copy.deepcopy(self.cat["offers"][0])
                offer["id"] = f"r{number}"
                offer["price_cents"] = rng.randint(0, 5000)
                offer["card_fee_cents"] = rng.choice((0, 0, 300))
                offer["facts"]["non_spicy"]["value"] = rng.choice((True, False, None))
                offer["facts"]["sugar_free_drink"]["value"] = rng.choice(
                    (True, False, None)
                )
                offer["facts"]["main_meat"]["value"] = rng.choice(("chicken", "beef"))
                if rng.randrange(5) == 0:
                    offer["coupon_key"] = "synthetic-coupon"
                cat["offers"].append(offer)
            expected = []
            for group in itertools.product(cat["offers"], repeat=len(req["people"])):
                if sum(bool(o.get("coupon_key")) for o in group) > 1:
                    continue
                if not all(
                    all(
                        o["facts"].get(k, {}).get("value") is v
                        for k, v in person["hard"].items()
                    )
                    for person, o in zip(req["people"], group)
                ):
                    continue
                price = sum(o["price_cents"] + o["card_fee_cents"] for o in group)
                if price <= req["budget_cents"]:
                    expected.append(price)
            result = compose(req, cat)
            with self.subTest(menu=index):
                self.assertEqual(bool(result.get("recommendations")), bool(expected))
                if expected:
                    self.assertEqual(
                        result["recommendations"][0]["total_cents"], min(expected)
                    )
                    for p in result["recommendations"]:
                        self.assertTrue(p["within_budget"])
                        self.assertLessEqual(p["total_cents"], req["budget_cents"])

    def test_C14_json_shape_mutations_never_escape_input_error(self) -> None:
        """Single-field JSON type mutations return a valid result or controlled InputError."""
        atoms = [
            None,
            False,
            True,
            0,
            1,
            1.25,
            "",
            "value",
            [],
            [None],
            {},
            {"x": None},
        ]
        for target in ("context", "budget_cents", "people"):
            for atom in atoms:
                value = copy.deepcopy(self.req)
                value[target] = atom
                with self.subTest(target=target, atom=atom):
                    try:
                        validate_request(value)
                    except InputError:
                        pass
        for target in ("mode", "as_of", "context", "offers"):
            for atom in atoms:
                value = copy.deepcopy(self.cat)
                value[target] = atom
                with self.subTest(target=target, atom=atom):
                    try:
                        validate_catalog(value)
                    except InputError:
                        pass

    def test_C15_corrupt_saved_recommendations_are_not_used_as_price_evidence(
        self,
    ) -> None:
        """Malformed saved plan containers and monetary types must not escape as a traceback."""
        good = compose(self.req, self.cat)
        variants = [
            None,
            {**good, "request_id": None},
            {**good, "recommendations": True},
            {**good, "recommendations": [None]},
        ]
        for change in [
            {"total_cents": "19.00"},
            {"total_cents": True},
            {"assignments": [None]},
            {"assignments": None},
        ]:
            value = copy.deepcopy(good)
            value["recommendations"][0].update(change)
            variants.append(value)
        for value in variants:
            with (
                self.subTest(case=variants.index(value)),
                self.assertRaises(InputError),
            ):
                compare_results(value, good)

    def test_C16_hard_constraints_are_checked_per_person_not_per_whole_basket(
        self,
    ) -> None:
        """A must receive chicken and B beef even when the reversed basket costs the same."""
        req = copy.deepcopy(self.req)
        req["people"][0]["hard"]["main_meat"] = "chicken"
        req["people"][1]["hard"]["main_meat"] = "beef"
        result = compose(req, self.cat)
        self.assertEqual(result["recommendations"][0]["total_cents"], 4300)
        chosen = {
            a["person_id"]: a["offer_id"]
            for a in result["recommendations"][0]["assignments"]
        }
        self.assertEqual(chosen, {"A": "demo-chicken", "B": "demo-beef"})
