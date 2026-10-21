"""同质组多重集合搜索（>6 人）：T-S2~T-S5、T-S6（上限单一定义）、T-S10（预飞行与性能护栏）。"""
import copy
import itertools
import re
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from meal_clear import config  # noqa: E402
from meal_clear.config import (  # noqa: E402
    COUPON_DETAIL_CAP,
    GROUP_ALLOCATIONS_CAP,
    LIMITS,
    RUN_CALL_CAP,
)
from meal_clear.grouping import (  # noqa: E402
    generate_candidates_grouped,
    group_allocations,
    group_people,
)
from meal_clear.ranking import compose_v2, generate_candidates, pareto_topk  # noqa: E402
from meal_clear.request_v2 import validate_request_v2  # noqa: E402
from scale_fixtures import (  # noqa: E402
    FOUR_GROUPS,
    distinct_people_request,
    group_request,
    make_catalog,
    make_offer,
    synth_catalog,
)

BANNED = ("全网最低", "一定买得到", "库存保证")


def best_of(generated, priority):
    """整单候选中最优者的 (总价, 偏好数)；无候选返回 None。"""
    whole = generated["whole"]
    if not whole:
        return None
    top = whole[0]
    return (top["hint_cents"], top["preference_matches"])


class GroupingStructureTests(unittest.TestCase):
    """T-S2：分组确定性、顺序无关、上限。"""

    def test_group_people_order_and_determinism(self):
        """首次出现顺序编号；组内保持请求顺序；打乱人员顺序后各组成员集合不变。"""
        req = validate_request_v2(group_request(FOUR_GROUPS))
        groups = group_people(req["people"])
        self.assertEqual([g["label"] for g in groups], ["G1", "G2", "G3", "G4"])
        self.assertEqual([len(g["people"]) for g in groups], [14, 13, 12, 11])
        for group in groups:
            ids = [p["id"] for p in group["people"]]
            self.assertEqual(ids, sorted(ids))
        self.assertEqual(
            [g["key"] for g in groups], [g["key"] for g in group_people(copy.deepcopy(req["people"]))]
        )
        shuffled = validate_request_v2(group_request(FOUR_GROUPS, shuffle_seed=5))
        self.assertNotEqual(
            [p["id"] for p in shuffled["people"]], [p["id"] for p in req["people"]]
        )
        as_sets = lambda gs: {g["key"]: frozenset(p["id"] for p in g["people"]) for g in gs}  # noqa: E731
        self.assertEqual(as_sets(groups), as_sets(group_people(shuffled["people"])))

    def test_allocations_capped_and_whole_within_width(self):
        """每组分配数 ≤48；整单候选数 ≤ 宽度；最优总价与人员顺序无关。"""
        catalog = synth_catalog()
        req = validate_request_v2(group_request(FOUR_GROUPS, limits={"max_whole_candidates": 50}))
        eligible = [
            e for e in generate_candidates(req, catalog)["per_person"][0]
        ]
        for size in (1, 2, 14, 50):
            allocs, floor, _ = group_allocations(eligible, size, "price")
            self.assertLessEqual(len(allocs), GROUP_ALLOCATIONS_CAP)
            self.assertGreaterEqual(floor, 0)
        generated = generate_candidates(req, catalog)
        self.assertLessEqual(len(generated["whole"]), 50)
        self.assertTrue(generated["stats"]["groups"] == 4)
        self.assertEqual(generated["stats"]["group_sizes"], [14, 13, 12, 11])
        shuffled = validate_request_v2(
            group_request(FOUR_GROUPS, shuffle_seed=3, limits={"max_whole_candidates": 50})
        )
        self.assertEqual(
            best_of(generated, "price"), best_of(generate_candidates(shuffled, catalog), "price")
        )

    def test_per_person_and_diagnostics_shape(self):
        """per_person 逐人一项；无可行选项的组产生一条诊断并按人数累计。"""
        catalog = make_catalog([make_offer("a", 1000, non_spicy=False)])
        req = validate_request_v2(group_request([(8, {"non_spicy": True}, {})]))
        generated = generate_candidates(req, catalog)
        self.assertEqual(generated["whole"], [])
        self.assertEqual(generated["stats"]["persons_without_option"], 8)
        self.assertEqual(len(generated["diagnostics"]), 1)
        diag = generated["diagnostics"][0]
        self.assertEqual(diag["person_id"], "P001")
        self.assertEqual(len(diag["person_ids"]), 8)
        self.assertEqual(len(generated["per_person"]), 8)


class GroupingAnswerTests(unittest.TestCase):
    """T-S3：闭式解与结构。"""

    def test_fifty_homogeneous_closed_form(self):
        """50 人同质：最低总价 = min(50×最低单价, 券款 + 49×最低单价)。"""
        catalog = synth_catalog()
        hard = {"non_spicy": True}
        req = group_request([(50, hard, {})])
        eligible = [o for o in catalog["offers"] if o["facts"].get("non_spicy", {}).get("value") is True]
        plain = min(o["price_cents"] + o["card_fee_cents"] for o in eligible if not o.get("coupon_key"))
        coupon = [o["price_cents"] + o["card_fee_cents"] for o in eligible if o.get("coupon_key")]
        expected = 50 * plain
        if coupon:
            expected = min(expected, min(coupon) + 49 * plain)
        result = compose_v2(req, catalog)
        self.assertEqual(result["ranking"][0]["total_cents"], expected)
        self.assertEqual(len(result["ranking"][0]["assignments"]), 50)

    def test_fifty_four_groups_structure(self):
        """50 人 4 组：Top3 结构、理由非空、声明含同质组与非全局最优；逐人分配完整且硬要求已核实。"""
        catalog = synth_catalog()
        result = compose_v2(group_request(FOUR_GROUPS), catalog)
        self.assertEqual(result["status"], "simulation")
        self.assertEqual(len(result["ranking"]), 3)
        totals = [i["total_cents"] for i in result["ranking"]]
        self.assertEqual(totals, sorted(totals))
        statement = result["ranking_meta"]["checked"]["statement"]
        self.assertIn("同质组", statement)
        self.assertIn("不是全局最优", statement)
        self.assertIn("50 位用餐者归并为 4 个同质组", statement)
        self.assertIn("最多 2 种不同套餐", statement)
        for item in result["ranking"]:
            self.assertTrue(item["reasons"])
            self.assertEqual(len(item["assignments"]), 50)
            self.assertTrue(
                all(c["status"] == "verified" for a in item["assignments"] for c in a["checks"])
            )
            text = "".join(item["reasons"])
            for phrase in BANNED:
                self.assertNotIn(phrase, text)
        self.assertRegex(statement, r"搜索空间 (约 \d\.\d×10\^\d+|\d+) 种")

    def test_search_space_scientific_notation_for_distinct_people(self):
        """50 个互不相同的人：搜索空间为超大整数，声明用科学计数法；小空间旧文案不变。"""
        catalog = synth_catalog()
        req = distinct_people_request(50)
        result = compose_v2(req, catalog)
        statement = result["ranking_meta"]["checked"]["statement"]
        self.assertRegex(statement, r"搜索空间 约 \d\.\d×10\^\d+ 种")
        self.assertIn("50 位用餐者归并为 50 个同质组", statement)
        self.assertGreater(result["ranking_meta"]["checked"]["search_space"], 10**30)
        small = compose_v2(group_request([(3, {"non_spicy": True}, {})]), catalog)
        self.assertRegex(small["ranking_meta"]["checked"]["statement"], r"搜索空间 \d+ 种")
        self.assertNotIn("同质组", small["ranking_meta"]["checked"]["statement"])


SCENARIOS = (
    # (种子, 组结构, 优先级, 是否收紧预算)
    (1, [(2, {"non_spicy": True}, {"main_meat": "beef"}), (2, {}, {})], "price", False),
    (2, [(3, {"non_spicy": True}, {"main_meat": "chicken"}), (3, {"sugar_free_drink": True}, {})], "price", True),
    (3, [(6, {"non_spicy": True}, {})], "price", True),
    (4, [(2, {"non_spicy": True}, {"main_meat": "pork"}), (4, {"non_spicy": True}, {"main_meat": "fish"})], "preference", False),
    (5, [(1, {}, {"main_meat": "beef"}), (2, {"non_spicy": True}, {"main_meat": "beef"}), (3, {}, {})], "preference", True),
    (6, [(4, {"non_spicy": True, "sugar_free_drink": True}, {"main_meat": "chicken"}), (2, {}, {"main_meat": "pork"})], "price", True),
    (7, [(5, {"non_spicy": True}, {"main_meat": "fish"}), (1, {"sugar_free_drink": True}, {})], "preference", True),
)


class DifferentialTests(unittest.TestCase):
    """T-S4：≤6 人场景下新旧路径最优 (总价, 偏好数) 相等。"""

    def test_grouped_matches_legacy_beam(self):
        """强制调用新路径与旧 beam；含券、含新购卡、含预算。"""
        for seed, groups, priority, tight in SCENARIOS:
            with self.subTest(seed=seed):
                catalog = synth_catalog(seed=seed)
                req = validate_request_v2(group_request(groups, priority=priority))
                legacy = generate_candidates(req, catalog, use_legacy_beam=True)
                if tight and legacy["whole"]:
                    req = validate_request_v2(
                        group_request(groups, budget=legacy["whole"][0]["hint_cents"] + 100, priority=priority)
                    )
                    legacy = generate_candidates(req, catalog, use_legacy_beam=True)
                grouped = generate_candidates_grouped(req, catalog)
                self.assertTrue(legacy["whole"])
                self.assertEqual(best_of(legacy, priority), best_of(grouped, priority))
                self.assertEqual(
                    legacy["stats"]["persons_without_option"],
                    grouped["stats"]["persons_without_option"],
                )

    def test_over_budget_matches_legacy(self):
        """预算低于全部方案时两条路径都没有候选。"""
        catalog = synth_catalog(seed=2)
        req = validate_request_v2(group_request([(3, {"non_spicy": True}, {})], budget=100))
        self.assertEqual(generate_candidates(req, catalog, use_legacy_beam=True)["whole"], [])
        self.assertEqual(generate_candidates_grouped(req, catalog)["whole"], [])


def brute_force_best(req, offers):
    """穷举每个人选哪个 offer（每单至多一张券），返回 {优先级: (总价, 偏好数)}。"""
    people = req["people"]
    best = {}
    for priority in ("price", "preference"):
        best_key = None
        for combo in itertools.product(offers, repeat=len(people)):
            if sum(1 for o in combo if o.get("coupon_key")) > 1:
                continue
            cost = sum(o["price_cents"] + o["card_fee_cents"] for o in combo)
            matches = sum(
                sum(o["facts"].get(k, {}).get("value") == v for k, v in p["prefer"].items())
                for p, o in zip(people, combo)
            )
            key = (cost, -matches) if priority == "price" else (-matches, cost)
            if best_key is None or key < best_key:
                best_key = key
        best[priority] = (
            (best_key[0], -best_key[1]) if priority == "price" else (best_key[1], -best_key[0])
        )
    return best


class BruteForceTests(unittest.TestCase):
    """T-S5：N≤4、K≤4 暴力枚举验证新路径最优。"""

    OFFERS = [
        make_offer("a", 1900, "chicken"),
        make_offer("b", 1700, "beef", card=900),
        make_offer("c", 2400, "beef", coupon="k1"),
        make_offer("d", 1500, "pork", coupon="k2"),
    ]

    def test_grouped_optimum_equals_enumeration(self):
        """多种组结构与两种优先级下，新路径最优 = 穷举最优。"""
        shapes = (
            [(4, {}, {"main_meat": "beef"})],
            [(2, {}, {"main_meat": "beef"}), (2, {}, {"main_meat": "pork"})],
            [(3, {}, {"main_meat": "chicken"}), (1, {}, {"main_meat": "beef"})],
            [(1, {}, {}), (1, {}, {"main_meat": "beef"}), (2, {}, {"main_meat": "pork"})],
            [(2, {}, {"main_meat": "beef"})],
            [(1, {}, {"main_meat": "pork"})],
        )
        catalog = make_catalog(self.OFFERS)
        for index, groups in enumerate(shapes):
            for priority in ("price", "preference"):
                with self.subTest(shape=index, priority=priority):
                    req = validate_request_v2(group_request(groups, priority=priority))
                    expected = brute_force_best(req, self.OFFERS)[priority]
                    generated = generate_candidates_grouped(req, catalog)
                    self.assertEqual(best_of(generated, priority), expected)


class LimitsAndGuardrailTests(unittest.TestCase):
    """T-S6 / T-S10。"""

    def test_max_people_defined_only_in_config(self):
        """MAX_PEOPLE 只在 config.py 赋值；校验文案由它生成。"""
        hits = []
        for path in (ROOT / "scripts").rglob("*.py"):
            if re.search(r"^\s*MAX_PEOPLE\s*=", path.read_text(encoding="utf-8"), re.M):
                hits.append(path.name)
        self.assertEqual(hits, ["config.py"])
        self.assertEqual(config.MAX_PEOPLE, 50)

    def test_preflight_call_budget_inequality(self):
        """最大 detail + 券详情 + 最大核价 + 其余 12 次 < 单次运行上限。"""
        total = LIMITS["max_detail_queries"][1] + COUPON_DETAIL_CAP + LIMITS["max_quotes"][1] + 12
        self.assertLess(total, RUN_CALL_CAP)

    def test_fifty_people_engine_time_guard(self):
        """50 人引擎（默认与上限参数）宽松时间上限，仅防回归；真实数字见性能记录。"""
        catalog = synth_catalog(60)
        for groups_req in (distinct_people_request(50), group_request(FOUR_GROUPS)):
            for limits in ({}, {"per_person_top_k": 12, "max_whole_candidates": 1000}):
                req = validate_request_v2({**groups_req, "limits": limits})
                start = time.perf_counter()
                generate_candidates(req, catalog)
                self.assertLess(time.perf_counter() - start, 20)

    def test_pareto_unaffected_by_group_count(self):
        """同一目录下组内候选与单人路径的 pareto_topk 一致（每组只算一次）。"""
        catalog = synth_catalog()
        req = validate_request_v2(group_request([(8, {"non_spicy": True}, {"main_meat": "beef"})]))
        generated = generate_candidates(req, catalog)
        first = generated["per_person"][0]
        for other in generated["per_person"][1:]:
            self.assertIs(other, first)
        self.assertTrue(first)
        self.assertEqual(pareto_topk(first, len(first), "price")[0][0]["id"], first[0]["id"])


if __name__ == "__main__":
    unittest.main()
