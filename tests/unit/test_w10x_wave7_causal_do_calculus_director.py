"""Wave 7: Pearl do-calculus, retention memory and the bounded director."""

from __future__ import annotations

import math

import pytest

from nexus_ai_agent.nagar.causal_memory.do_calculus import (
    AverageCausalEffect,
    CausalEffectError,
    CausalGraph,
    CausalMemory,
    ConditionalProbabilityTable,
    Episode,
    ReadOnlyCausalMemory,
    RetentionRecord,
    StructuralCausalModel,
    adjustment_distribution,
    average_causal_effect,
    is_d_separated,
    is_valid_backdoor_set,
    retention,
)
from nexus_ai_agent.nagar.director.bounded_director import (
    BoundedDirector,
    CreativeAction,
    DirectorBudget,
    DirectorPlan,
    DirectorPlanningError,
)


def _cpt(
    variable: str, parents: tuple[str, ...], table: dict[tuple[str, ...], dict[str, float]]
) -> ConditionalProbabilityTable:
    return ConditionalProbabilityTable(variable, parents, table)


def simpson_model() -> StructuralCausalModel:
    graph = CausalGraph(("X", "Y", "Z"), (("Z", "X"), ("Z", "Y"), ("X", "Y")))
    domains = {"X": ("0", "1"), "Y": ("0", "1"), "Z": ("0", "1")}
    cpts = {
        "Z": _cpt("Z", (), {(): {"0": 0.5, "1": 0.5}}),
        "X": _cpt(
            "X",
            ("Z",),
            {("0",): {"0": 0.8, "1": 0.2}, ("1",): {"0": 0.2, "1": 0.8}},
        ),
        "Y": _cpt(
            "Y",
            ("X", "Z"),
            {
                ("0", "0"): {"0": 0.10, "1": 0.90},
                ("0", "1"): {"0": 0.95, "1": 0.05},
                ("1", "0"): {"0": 0.05, "1": 0.95},
                ("1", "1"): {"0": 0.90, "1": 0.10},
            },
        ),
    }
    return StructuralCausalModel(graph, cpts, domains)


def test_graph_structure_and_validation() -> None:
    graph = CausalGraph(("X", "Y", "Z"), (("Z", "X"), ("Z", "Y"), ("X", "Y")))
    assert graph.parents("Y") == ("X", "Z")
    assert graph.children("Z") == ("X", "Y")
    assert graph.ancestors(("Y",)) == frozenset({"Y", "X", "Z"})
    assert graph.descendants("Z") == frozenset({"X", "Y"})
    assert graph.topological_order() == ("Z", "X", "Y")
    mutilated = graph.without_outgoing("X")
    assert ("X", "Y") not in mutilated.edges
    with pytest.raises(CausalEffectError):
        graph.parents("Q")
    with pytest.raises(CausalEffectError):
        graph.descendants("Q")
    with pytest.raises(CausalEffectError):
        graph.ancestors(("Q",))
    with pytest.raises(CausalEffectError):
        CausalGraph((), ())
    with pytest.raises(CausalEffectError):
        CausalGraph(("X", "X"), ())
    with pytest.raises(CausalEffectError):
        CausalGraph(("",), ())
    with pytest.raises(CausalEffectError):
        CausalGraph(("X",), (("X", "Y"),))
    with pytest.raises(CausalEffectError):
        CausalGraph(("X",), (("X", "X"),))
    with pytest.raises(CausalEffectError):
        CausalGraph(("X", "Y"), (("X", "Y"), ("X", "Y")))
    with pytest.raises(CausalEffectError):
        CausalGraph(("X", "Y"), (("X", "Y"), ("Y", "X")))


def test_cpt_validation_and_probability() -> None:
    cpt = _cpt("X", (), {(): {"0": 0.3, "1": 0.7}})
    assert cpt.probability("1", {}) == pytest.approx(0.7)
    with pytest.raises(CausalEffectError):
        cpt.probability("2", {})
    child = _cpt("X", ("Z",), {("0",): {"0": 1.0}, ("1",): {"0": 1.0}})
    with pytest.raises(CausalEffectError):
        child.probability("0", {})
    with pytest.raises(CausalEffectError):
        _cpt("X", ("Y", "Z"), {(("0",),): {"0": 1.0}})
    with pytest.raises(CausalEffectError):
        _cpt("X", ("Z", "Y"), {(("0", "0"),): {"0": 1.0}})
    with pytest.raises(CausalEffectError):
        _cpt("", (), {(): {"0": 1.0}})
    with pytest.raises(CausalEffectError):
        _cpt("X", (), {(): {"": 1.0}})
    with pytest.raises(CausalEffectError):
        _cpt("X", (), {(): {"0": -0.1, "1": 1.1}})
    with pytest.raises(CausalEffectError):
        _cpt("X", (), {(): {"0": 0.2, "1": 0.2}})
    with pytest.raises(CausalEffectError):
        _cpt("X", (), {})


def test_scm_validation() -> None:
    model = simpson_model()
    with pytest.raises(TypeError):
        StructuralCausalModel("graph", {}, {})  # type: ignore[arg-type]
    graph = CausalGraph(("X",), ())
    good = _cpt("X", (), {(): {"0": 1.0}})
    with pytest.raises(CausalEffectError):
        StructuralCausalModel(graph, {}, {"X": ("0",)})
    with pytest.raises(CausalEffectError):
        StructuralCausalModel(graph, {"X": good}, {})
    with pytest.raises(CausalEffectError):
        StructuralCausalModel(graph, {"X": good}, {"X": ()})
    with pytest.raises(CausalEffectError):
        StructuralCausalModel(graph, {"X": good}, {"X": ("0", "0")})
    with pytest.raises(CausalEffectError):
        StructuralCausalModel(graph, {"X": good}, {"X": ("",)})
    with pytest.raises(TypeError):
        StructuralCausalModel(graph, {"X": "x"}, {"X": ("0",)})  # type: ignore[dict-item]
    with pytest.raises(CausalEffectError):
        StructuralCausalModel(graph, {"X": _cpt("Y", (), {(): {"0": 1.0}})}, {"X": ("0",)})
    with pytest.raises(CausalEffectError):
        StructuralCausalModel(
            graph, {"X": _cpt("X", ("Q",), {(("0",),): {"0": 1.0}})}, {"X": ("0",)}
        )
    with pytest.raises(CausalEffectError):
        StructuralCausalModel(graph, {"X": _cpt("X", (), {(): {"1": 1.0}})}, {"X": ("0",)})
    assert model.graph.nodes == ("X", "Y", "Z")


def test_joint_and_observational_distribution() -> None:
    model = simpson_model()
    total = sum(model.joint(assignment) for assignment in model._enumerate({}))
    assert total == pytest.approx(1.0)
    with pytest.raises(CausalEffectError):
        model.joint({"X": "0"})
    with pytest.raises(CausalEffectError):
        model.observational_distribution("Q")
    with pytest.raises(CausalEffectError):
        model.observational_distribution("Y", {"Q": "0"})
    with pytest.raises(CausalEffectError):
        model.observational_distribution("Y", {"Y": "0"})
    observed = model.observational_distribution("Y", {"X": "1"})
    assert sum(observed.values()) == pytest.approx(1.0)
    assert observed["1"] == pytest.approx(0.27, abs=1e-9)
    with pytest.raises(CausalEffectError):
        model.observational_distribution("Y", {"X": "2"})


def test_d_separation_chain_fork_collider() -> None:
    chain = CausalGraph(("X", "M", "Y"), (("X", "M"), ("M", "Y")))
    assert is_d_separated(chain, "X", "Y", set()) is False
    assert is_d_separated(chain, "X", "Y", {"M"}) is True
    fork = CausalGraph(("X", "Y", "Z"), (("Z", "X"), ("Z", "Y")))
    assert is_d_separated(fork, "X", "Y", set()) is False
    assert is_d_separated(fork, "X", "Y", {"Z"}) is True
    collider = CausalGraph(("X", "Y", "C"), (("X", "C"), ("Y", "C")))
    assert is_d_separated(collider, "X", "Y", set()) is True
    assert is_d_separated(collider, "X", "Y", {"C"}) is False
    assert is_d_separated(chain, "X", "X", set()) is False
    assert is_d_separated(chain, "X", "Y", {"X"}) is True
    with pytest.raises(TypeError):
        is_d_separated("chain", "X", "Y", set())  # type: ignore[arg-type]
    with pytest.raises(CausalEffectError):
        is_d_separated(chain, "Q", "Y", set())
    with pytest.raises(CausalEffectError):
        is_d_separated(chain, "X", "Y", {"Q"})


def test_backdoor_criterion() -> None:
    model = simpson_model()
    graph = model.graph
    assert is_valid_backdoor_set(graph, "X", "Y", {"Z"}) is True
    assert is_valid_backdoor_set(graph, "X", "Y", set()) is False
    assert is_valid_backdoor_set(graph, "X", "Y", {"X"}) is False
    assert is_valid_backdoor_set(graph, "X", "Y", {"Y"}) is False
    with pytest.raises(CausalEffectError):
        is_valid_backdoor_set(graph, "X", "Y", {"Q"})
    with pytest.raises(CausalEffectError):
        is_valid_backdoor_set(graph, "X", "X", set())


def test_adjustment_equals_intervention_and_simpson() -> None:
    model = simpson_model()
    adjusted = adjustment_distribution(model, "X", "1", "Y", {"Z"})
    intervened = model.interventional_distribution("Y", {"X": "1"})
    assert adjusted == pytest.approx(dict(intervened))
    with pytest.raises(CausalEffectError):
        adjustment_distribution(model, "X", "1", "Y", set())
    with pytest.raises(CausalEffectError):
        adjustment_distribution(model, "X", "2", "Y", {"Z"})

    effect = average_causal_effect(model, "X", "Y", "1", "0")
    assert effect.ace == pytest.approx(0.05, abs=1e-9)
    assert effect.treated_mean > effect.control_mean
    observational_treated = model.observational_distribution("Y", {"X": "1"})["1"]
    observational_control = model.observational_distribution("Y", {"X": "0"})["1"]
    assert observational_treated < observational_control  # Simpson's reversal
    assert effect.ace > 0.0


def test_interventional_validation() -> None:
    model = simpson_model()
    with_evidence = model.interventional_distribution("Y", {"X": "1"}, {"Z": "0"})
    assert sum(with_evidence.values()) == pytest.approx(1.0)
    with pytest.raises(CausalEffectError):
        model.interventional_distribution("Y", {})
    with pytest.raises(CausalEffectError):
        model.interventional_distribution("Q", {"X": "1"})
    with pytest.raises(CausalEffectError):
        model.interventional_distribution("Y", {"X": "1"}, {"X": "0"})
    with pytest.raises(CausalEffectError):
        model.interventional_distribution("Y", {"X": "2"})
    with pytest.raises(CausalEffectError):
        AverageCausalEffect(math.nan, 0.0, 0.0)
    assert AverageCausalEffect(0.5, 0.4, 0.1).ace == pytest.approx(0.1)


# ---------------------------------------------------------------- memory / retention


def test_retention_law() -> None:
    episode = Episode("e1", 1.0, observed_at_days=0.0, retrievals=0)
    assert retention(episode, 0.0) == pytest.approx(1.0)
    assert retention(episode, 10.0) < 1e-4
    rehearsed = Episode("e2", 1.0, observed_at_days=0.0, retrievals=3)
    assert retention(rehearsed, 3.0) > retention(episode, 3.0)
    assert retention(episode, 2.0, stability=4.0) > retention(episode, 2.0)
    with pytest.raises(TypeError):
        retention("e1", 1.0)  # type: ignore[arg-type]
    with pytest.raises(CausalEffectError):
        retention(episode, -1.0)
    with pytest.raises(CausalEffectError):
        retention(episode, 1.0, stability=0.0)
    with pytest.raises(CausalEffectError):
        retention(episode, 1.0, alpha=0.0)
    with pytest.raises(CausalEffectError):
        retention(episode, 1.0, beta=0.0)


def test_episode_validation() -> None:
    with pytest.raises(CausalEffectError):
        Episode("", 1.0, 0.0)
    with pytest.raises(CausalEffectError):
        Episode("e", math.nan, 0.0)
    with pytest.raises(CausalEffectError):
        Episode("e", -1.0, 0.0)
    with pytest.raises(CausalEffectError):
        Episode("e", 1.0, -1.0)
    with pytest.raises(TypeError):
        Episode("e", 1.0, 0.0, True)  # type: ignore[arg-type]
    with pytest.raises(CausalEffectError):
        Episode("e", 1.0, 0.0, -1)
    with pytest.raises(CausalEffectError):
        RetentionRecord("e", 1.1, 0.0, 0)
    with pytest.raises(CausalEffectError):
        RetentionRecord("e", 0.5, -1.0, 0)
    with pytest.raises(TypeError):
        RetentionRecord("e", 0.5, 0.0, True)  # type: ignore[arg-type]


def test_causal_memory_is_append_only() -> None:
    original = CausalMemory()
    first = original.record(Episode("a", 2.0, 0.0))
    second = first.record(Episode("b", 1.0, 1.0, 1))
    assert original.episodes == ()
    assert len(first.episodes) == 1
    assert len(second.episodes) == 2
    assert second.get("b") is not None
    assert second.get("missing") is None
    with pytest.raises(TypeError):
        original.record("x")  # type: ignore[arg-type]
    with pytest.raises(CausalEffectError):
        CausalMemory((Episode("a", 1.0, 0.0), Episode("a", 1.0, 0.0)))
    with pytest.raises(TypeError):
        CausalMemory(("x",))  # type: ignore[arg-type]
    records = second.retention_records(2.0)
    assert [record.episode_id for record in records] == ["a", "b"]
    assert all(0.0 <= record.retention <= 1.0 for record in records)
    assert second.query(2.0, minimum_retention=0.0) == second.episodes
    assert second.query(2.0, minimum_retention=0.99) == ()
    with pytest.raises(CausalEffectError):
        second.query(2.0, minimum_retention=1.1)


def test_read_only_proxy() -> None:
    memory = CausalMemory().record(Episode("a", 1.0, 0.0))
    proxy = ReadOnlyCausalMemory(memory)
    assert proxy.episodes == memory.episodes
    assert proxy.get("a") == memory.episodes[0]
    assert proxy.query(0.0, minimum_retention=0.0) == memory.episodes
    assert proxy.retention_records(0.0)[0].episode_id == "a"
    assert not hasattr(proxy, "record")
    with pytest.raises(AttributeError):
        proxy.extra = 1  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        ReadOnlyCausalMemory("memory")  # type: ignore[arg-type]


# ---------------------------------------------------------------- director


def test_budget_and_action_validation() -> None:
    DirectorBudget(2, 10.0, 30.0)
    with pytest.raises(TypeError):
        DirectorBudget(True, 10.0, 30.0)  # type: ignore[arg-type]
    for kwargs in (
        {"max_actions": 0, "max_cost": 10.0, "horizon_days": 1.0},
        {"max_actions": 1, "max_cost": 0.0, "horizon_days": 1.0},
        {"max_actions": 1, "max_cost": 10.0, "horizon_days": 0.0},
        {"max_actions": 1, "max_cost": math.nan, "horizon_days": 1.0},
    ):
        with pytest.raises(DirectorPlanningError):
            DirectorBudget(**kwargs)
    CreativeAction("a", 1.0, 1.0)
    for kwargs in (
        {"action_id": "", "expected_utility": 1.0, "cost": 1.0},
        {"action_id": "a", "expected_utility": -1.0, "cost": 1.0},
        {"action_id": "a", "expected_utility": math.nan, "cost": 1.0},
        {"action_id": "a", "expected_utility": 1.0, "cost": -1.0},
        {"action_id": "a", "expected_utility": 1.0, "cost": 1.0, "evidence_id": ""},
        {"action_id": "a", "expected_utility": 1.0, "cost": 1.0, "required_retention": 2.0},
    ):
        with pytest.raises(DirectorPlanningError):
            CreativeAction(**kwargs)


def test_bounded_director_planning() -> None:
    memory = CausalMemory().record(Episode("e1", 1.0, 0.0))
    director = BoundedDirector(DirectorBudget(max_actions=2, max_cost=10.0, horizon_days=5.0))
    actions = (
        CreativeAction("a", 10.0, 4.0, evidence_id="e1", required_retention=0.1),
        CreativeAction("b", 6.0, 2.0),
        CreativeAction("c", 10.0, 8.0),
    )
    plan = director.plan(memory, actions, now_days=1.0)
    assert [action.action_id for action in plan.actions] == ["b", "a"]
    assert plan.total_utility == pytest.approx(16.0)
    assert plan.total_cost == pytest.approx(6.0)
    assert plan.remaining_budget == pytest.approx(4.0)
    assert plan.skipped == ("c",)
    assert director.budget.max_actions == 2
    with pytest.raises(TypeError):
        BoundedDirector("budget")  # type: ignore[arg-type]


def test_director_skips_stale_or_missing_evidence() -> None:
    memory = CausalMemory().record(Episode("e1", 1.0, 0.0))
    director = BoundedDirector(DirectorBudget(max_actions=5, max_cost=10.0, horizon_days=5.0))
    actions = (
        CreativeAction("missing", 100.0, 1.0, evidence_id="nope"),
        CreativeAction("stale", 50.0, 1.0, evidence_id="e1", required_retention=0.0),
        CreativeAction("fresh", 1.0, 1.0),
    )
    plan = director.plan(memory, actions, now_days=10.0)
    assert [action.action_id for action in plan.actions] == ["fresh"]
    assert plan.skipped == ("missing", "stale")

    low_retention = BoundedDirector(DirectorBudget(5, 10.0, 5.0))
    plan = low_retention.plan(
        memory,
        (CreativeAction("e", 1.0, 1.0, evidence_id="e1", required_retention=0.99),),
        now_days=4.0,
    )
    assert plan.actions == ()
    assert plan.skipped == ("e",)


def test_director_skips_over_budget_and_cumulative_cost() -> None:
    memory = CausalMemory()
    director = BoundedDirector(DirectorBudget(max_actions=5, max_cost=5.0, horizon_days=5.0))
    plan = director.plan(
        memory,
        (
            CreativeAction("too_dear", 100.0, 20.0),
            CreativeAction("first", 10.0, 4.0),
            CreativeAction("second", 1.0, 4.0),
        ),
        now_days=0.0,
    )
    assert [action.action_id for action in plan.actions] == ["first"]
    assert plan.skipped == ("too_dear", "second")
    assert plan.remaining_budget == pytest.approx(1.0)


def test_director_zero_cost_and_proxy() -> None:
    memory = CausalMemory().record(Episode("e1", 1.0, 0.0))
    proxy = ReadOnlyCausalMemory(memory)
    director = BoundedDirector(DirectorBudget(1, 5.0, 10.0))
    plan = director.plan(
        proxy,
        (CreativeAction("free", 2.0, 0.0), CreativeAction("paid", 100.0, 1.0)),
        now_days=0.0,
    )
    assert [action.action_id for action in plan.actions] == ["free"]
    assert plan.skipped == ("paid",)
    with pytest.raises(TypeError):
        director.plan("memory", (), 0.0)  # type: ignore[arg-type]
    with pytest.raises(DirectorPlanningError):
        director.plan(memory, (), -1.0)
    with pytest.raises(TypeError):
        director.plan(memory, ("x",), 0.0)  # type: ignore[arg-type]


def test_plan_validation() -> None:
    action = CreativeAction("a", 1.0, 0.5)
    DirectorPlan((action,), (), 1.0, 0.5, 4.5)
    with pytest.raises(TypeError):
        DirectorPlan(("a",), (), 0.0, 0.0, 0.0)  # type: ignore[arg-type]
    with pytest.raises(DirectorPlanningError):
        DirectorPlan((action,), (), 2.0, 0.5, 4.5)
    with pytest.raises(DirectorPlanningError):
        DirectorPlan((action,), (), 1.0, 2.0, 4.5)
    with pytest.raises(DirectorPlanningError):
        DirectorPlan((), (), -1.0, 0.0, 0.0)
