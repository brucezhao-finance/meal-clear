"""多人多方案排名引擎（离线、合成数据）：T-A1~A8 离线部分、T-E1~E6。"""
import copy
import json
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from meal_clear.core import InputError, validate_request  # noqa: E402
from meal_clear.ranking import (  # noqa: E402
    basket_signature,
    compose_v2,
    generate_candidates,
    pareto_topk,
    rank_candidates,
    select_for_quote,
)
from meal_clear.request_v2 import validate_request_v2  # noqa: E402

CTX = {"store_code": "T1", "meal_time": "t", "fulfillment": "pickup", "be_code": None}
BANNED = ("全网最低", "一定买得到", "库存保证")


def offer(oid, price, meat="chicken", non_spicy=True, sugar_free=True, card=0, coupon=None, wire=None):
    """构造合成 offer。"""
    facts = {"main_meat": {"value": meat, "source": "合成"}}
    if non_spicy is not None:
        facts["non_spicy"] = {"value": non_spicy, "source": "合成"}
    facts["sugar_free_drink"] = {"value": sugar_free, "source": "合成"}
    result = {
        "id": oid,
        "name": "餐" + oid,
        "price_cents": price,
        "card_fee_cents": card,
        "facts": facts,
        "items": [
            {"code": oid + "-m", "name": "主" + oid, "quantity": 1, "role": "main"},
            {"code": oid + "-d", "name": "饮" + oid, "quantity": 1, "role": "drink"},
        ],
    }
    if coupon:
        result["coupon_key"] = coupon
        result["coupon_title"] = "券" + coupon
    if wire:
        result["wire_items"] = wire
    return result


def catalog(offers, context=None, mode="simulation"):
    """构造合成菜单。"""
    return {
        "mode": mode,
        "as_of": "合成时间",
        "context": context or dict(CTX),
        "order_fee_cents": 0,
        "price_source": "合成价目",
        "offers": offers,
    }


def request(people=1, budget=100000, hard=None, prefer=None, **extra):
    """构造 v2 请求。"""
    base = {
        "schema_version": 2,
        "context": {"store_code": "T1", "meal_time": "t"},
        "budget_cents": budget,
        "people": [
            {
                "id": chr(65 + i),
                "hard": dict(hard if hard is not None else {"non_spicy": True}),
                "prefer": dict(prefer if prefer is not None else {}),
            }
            for i in range(people)
        ],
    }
    base.update(extra)
    return base


def varied_offers():
    """多肉类、多价位、含券、含新卡、含未知的合成菜单。"""
    return [
        offer("o1", 1900, "chicken"),
        offer("o2", 1700, "chicken", sugar_free=False),
        offer("o3", 2400, "beef"),
        offer("o4", 1800, "pork"),
        offer("o5", 2200, "fish"),
        offer("o6", 1600, "chicken", card=900),
        offer("o7", 1750, "chicken", coupon="c1"),
        offer("o8", 2300, "beef", coupon="c1"),
        offer("o9", 1000, "pork", non_spicy=None),
        offer("o10", 1500, "chicken", non_spicy=False),
    ]


class MultiRankingTests(unittest.TestCase):
    """人数、上限、排序与理由。"""

    def test_people_counts_each_get_assignment_and_exact_total(self):
        """T-A1~A4：1/2/3/6 人均有排名，每人有分配，总价为各 offer 价之和。"""
        offers = varied_offers()
        prices = {o["id"]: o["price_cents"] + o["card_fee_cents"] for o in offers}
        for n in (1, 2, 3, 6):
            req = request(n, prefer={"main_meat": "chicken"})
            result = compose_v2(req, catalog(offers))
            self.assertTrue(result["ranking"], n)
            self.assertEqual(result["ranking"], result["recommendations"])
            for item in result["ranking"]:
                self.assertEqual(
                    [a["person_id"] for a in item["assignments"]],
                    [p["id"] for p in req["people"]],
                )
                self.assertEqual(
                    item["total_cents"],
                    sum(prices[a["offer_id"]] for a in item["assignments"]),
                )
            self.assertLessEqual(
                result["ranking_meta"]["checked"]["whole_generated"], 200
            )

    def test_people_bounds_and_duplicate_ids(self):
        """T-A5/A6/S6：0/51 人报 1至50位；1/50 人通过；v1 三人仍报 1或2位；重复 id 报错。"""
        for n in (0, 51):
            with self.assertRaises(InputError) as ctx:
                validate_request_v2(request(n))
            self.assertIn("1至50位", str(ctx.exception))
        for n in (1, 50):
            self.assertEqual(len(validate_request_v2(request(n))["people"]), n)
        v1 = {
            "context": {"store_code": "A", "channel": "pickup", "meal_time": "now"},
            "budget_cents": 100,
            "people": [{"id": c} for c in "ABC"],
        }
        with self.assertRaises(InputError) as ctx:
            validate_request(v1)
        self.assertIn("1或2位", str(ctx.exception))
        dup = request(2)
        dup["people"][1]["id"] = "A"
        with self.assertRaises(InputError) as ctx:
            validate_request_v2(dup)
        self.assertIn("id 重复", str(ctx.exception))

    def test_candidate_cap_truncates_with_honest_statement(self):
        """T-A7：候选上限触发 → whole_truncated，声明不是全局最优，无禁用词。"""
        req = request(6, limits={"max_whole_candidates": 10})
        result = compose_v2(req, catalog(varied_offers()))
        checked = result["ranking_meta"]["checked"]
        self.assertTrue(checked["whole_truncated"])
        self.assertLessEqual(checked["whole_generated"], 10)
        self.assertIn("不是全局最优", checked["statement"])
        dump = json.dumps(result, ensure_ascii=False)
        for word in BANNED:
            self.assertNotIn(word, dump)

    def test_determinism_with_shuffled_offers(self):
        """T-E1：连续 20 次、打乱 offers 顺序，signature 与总价序列一致。"""
        req = request(3, prefer={"main_meat": "chicken"})
        baseline = None
        rng = random.Random(7)
        for _ in range(20):
            offers = varied_offers()
            rng.shuffle(offers)
            result = compose_v2(req, catalog(offers))
            seq = [(i["signature"], i["total_cents"]) for i in result["ranking"]]
            if baseline is None:
                baseline = seq
            self.assertEqual(seq, baseline)

    def test_tie_break_by_offer_id_and_reason(self):
        """T-E2：总价/偏好全相同 → 按 offer id 字典序，理由说明稳定排序。"""
        offers = [offer("b-same", 1000), offer("a-same", 1000), offer("c-same", 1000)]
        result = compose_v2(request(1), catalog(offers))
        self.assertEqual(
            [i["assignments"][0]["offer_id"] for i in result["ranking"]],
            ["a-same", "b-same", "c-same"],
        )
        self.assertTrue(any("稳定排序" in r for r in result["ranking"][1]["reasons"]))

    def test_price_vs_preference_priority(self):
        """T-E3：两种 priority 在专门构造的数据上第 1 名不同，理由分别含“最低/最多”。"""
        offers = [offer("cheap", 1000, "pork"), offer("liked", 1500, "chicken")]
        base = request(1, prefer={"main_meat": "chicken"})
        by_price = compose_v2(base, catalog(offers))
        pref_req = copy.deepcopy(base)
        pref_req["ranking"] = {"priority": "preference"}
        by_pref = compose_v2(pref_req, catalog(offers))
        self.assertEqual(by_price["ranking"][0]["assignments"][0]["offer_id"], "cheap")
        self.assertEqual(by_pref["ranking"][0]["assignments"][0]["offer_id"], "liked")
        self.assertTrue(any("最低" in r for r in by_price["ranking"][0]["reasons"]))
        self.assertTrue(any("最多" in r for r in by_pref["ranking"][0]["reasons"]))

    def test_unknown_candidate_never_ranked(self):
        """T-E4：注入 unknown 候选 → 进入 excluded.hard_not_verified。"""
        req = validate_request_v2(request(2))
        cat = catalog(varied_offers())
        cands = generate_candidates(req, cat)["whole"]
        poisoned = copy.deepcopy(cands[0])
        poisoned["assignments"][0]["checks"][0]["status"] = "unknown"
        ranked = rank_candidates(req, cat, [poisoned], None)
        self.assertEqual(ranked["ranked"], [])
        self.assertEqual(ranked["excluded"]["hard_not_verified"], 1)
        # 缺少 non_spicy 证据的 offer 在生成阶段即被拒，不会出现在任何分配中
        result = compose_v2(request(2), cat)
        used = {a["offer_id"] for i in result["ranking"] for a in i["assignments"]}
        self.assertNotIn("o9", used)

    def test_no_result_four_categories(self):
        """T-E5：四类无推荐原因及对应 v1 状态。"""
        offers = varied_offers()
        wrong = dict(CTX, store_code="OTHER")
        r1 = compose_v2(request(2), catalog(offers, context=wrong))
        self.assertEqual((r1["status"], r1["no_result"]["category"]), ("context_mismatch",) * 2)
        r2 = compose_v2(request(2, hard={"main_meat": "tofu"}), catalog(offers))
        self.assertEqual(r2["status"], "no_verified_plan")
        self.assertEqual(r2["no_result"]["category"], "attribute_unknown")
        unpriced = [offer("u1", None), offer("u2", None)]
        r3 = compose_v2(request(2), catalog(unpriced))
        self.assertEqual(r3["status"], "needs_price_verification")
        self.assertEqual(r3["no_result"]["category"], "needs_price_verification")
        r4 = compose_v2(request(2, budget=1), catalog(offers))
        self.assertEqual(r4["status"], "over_budget_in_checked_set")
        self.assertEqual(r4["no_result"]["category"], "over_budget_in_checked_set")
        for r in (r1, r2, r3, r4):
            self.assertEqual(r["recommendations"], [])
            self.assertIsNotNone(r["no_result"]["detail"])

    def test_gap_and_reasons(self):
        """T-E6：差价正确；第 2 名理由含“贵 ¥”；无禁用词。"""
        offers = [offer("p1", 1000), offer("p2", 1250), offer("p3", 1500)]
        result = compose_v2(request(1), catalog(offers))
        gaps = [i["gap_vs_first_cents"] for i in result["ranking"]]
        self.assertEqual(gaps, [0, 250, 500])
        self.assertTrue(any("贵 ¥2.50" in r for r in result["ranking"][1]["reasons"]))
        for item in result["ranking"]:
            for reason in item["reasons"]:
                for word in BANNED:
                    self.assertNotIn(word, reason)

    def test_coupon_at_most_one_and_no_codes_in_output(self):
        """每单至多一张券；wire_items/券码不进入输出。"""
        wire = [{"productCode": "X", "quantity": 1, "couponCode": "COUPONCODE-SENTINEL-9"}]
        offers = [
            offer("k1", 900, coupon="c1", wire=wire),
            offer("k2", 900, coupon="c2", wire=wire),
            offer("n1", 2000),
        ]
        result = compose_v2(request(2), catalog(offers))
        for item in result["ranking"]:
            used = [a["offer_id"] for a in item["assignments"]]
            self.assertLessEqual(sum(1 for u in used if u.startswith("k")), 1)
        dump = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("SENTINEL", dump)
        self.assertNotIn("wire_items", dump)
        self.assertNotIn("coupon_key", dump)

    def test_pareto_and_basket_signature_and_quote_selection(self):
        """帕累托去掉被支配项；购物篮签名与顺序无关；核价选样去重且不超限。"""
        req = validate_request_v2(request(1))
        cat = catalog([offer("d1", 2000), offer("d2", 1000), offer("d3", 1000, card=0)])
        entries = generate_candidates(req, cat)["per_person"][0]
        self.assertTrue(entries)
        a, b = offer("x", 1), offer("y", 2)
        self.assertEqual(basket_signature([a, b]), basket_signature([b, a]))
        req6 = validate_request_v2(request(3))
        whole = generate_candidates(req6, catalog(varied_offers()))["whole"]
        picked = select_for_quote(whole, 5, "price")
        self.assertLessEqual(len(picked), 5)
        self.assertEqual(len({c["signature"] for c in picked}), len(picked))
        # 直接测 pareto：d1 被 d2 支配
        pool = [
            {"id": "d1", "cost": 2000, "matches": 0, "card": 0, "coupon": 0},
            {"id": "d2", "cost": 1000, "matches": 0, "card": 0, "coupon": 0},
            {"id": "d4", "cost": 1500, "matches": 1, "card": 0, "coupon": 0},
            {"id": "d5", "cost": 3000, "matches": 0, "card": 0, "coupon": 1},
        ]
        top, cut = pareto_topk(pool, 2, "price")
        self.assertEqual([e["id"] for e in top], ["d2", "d4"])
        self.assertTrue(cut)
        top, cut = pareto_topk(pool, 3, "price")
        self.assertEqual([e["id"] for e in top], ["d2", "d4", "d5"])
        self.assertFalse(cut)

    def test_diagnostics_for_person_without_option_and_no_mutation(self):
        """某人无可行 offer 时给出逐人诊断；compose_v2 不修改入参。"""
        req = request(2)
        req["people"][1]["hard"] = {"main_meat": "tofu"}
        cat = catalog(varied_offers())
        before = (copy.deepcopy(req), copy.deepcopy(cat))
        result = compose_v2(req, cat)
        self.assertEqual((req, cat), before)
        self.assertEqual(result["diagnostics"][0]["person_id"], "B")


if __name__ == "__main__":
    unittest.main()
