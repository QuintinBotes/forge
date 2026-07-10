"""Unit tests for the pure F40 PM-depth math: capacity, estimation scales,
portfolio CFD/cycle-lead-time, and sprint-goal alignment.

These are all deterministic, no-I/O modules (mirrors ``test_velocity.py`` /
``test_burndown.py``'s style for the F26 pure math they extend).
"""

from __future__ import annotations

from datetime import date, datetime

from forge_board.capacity import (
    MemberAssignment,
    MemberCapacityInput,
    compute_capacity_report,
)
from forge_board.estimation import EstimationScale, is_valid_estimate, nearest_scale_value
from forge_board.goal_alignment import TaskAlignmentInput, compute_goal_alignment
from forge_board.portfolio import (
    TaskStatusEventInput,
    average_cycle_lead_time,
    compute_cfd,
    compute_cycle_lead_time,
    compute_portfolio_velocity,
)
from forge_board.velocity import VelocitySummary

# --------------------------------------------------------------------------- #
# capacity                                                                     #
# --------------------------------------------------------------------------- #


def test_capacity_over_under_balanced() -> None:
    capacities = [
        MemberCapacityInput(member_id="alice", capacity_points=5),
        MemberCapacityInput(member_id="bob", capacity_points=8),
        MemberCapacityInput(member_id="carol", capacity_points=6),
    ]
    assignments = [
        MemberAssignment(member_id="alice", points=3),
        MemberAssignment(member_id="alice", points=5),  # alice: 8/5 = over
        MemberAssignment(member_id="bob", points=2),  # bob: 2/8 = under
        MemberAssignment(member_id="carol", points=6),  # carol: 6/6 = balanced
    ]
    report = compute_capacity_report(capacities, assignments)
    by_member = {r.member_id: r for r in report}
    assert by_member["alice"].assigned_points == 8
    assert by_member["alice"].status == "over"
    assert by_member["bob"].status == "under"
    assert by_member["carol"].status == "balanced"


def test_capacity_zero_declared_with_assignment_is_balanced() -> None:
    # No declared capacity but assigned work: utilization defaults to 1.0
    # (a neutral "fully loaded" signal), which falls inside the balanced band.
    report = compute_capacity_report([], [MemberAssignment(member_id="dave", points=3)])
    assert report[0].utilization == 1.0
    assert report[0].status == "balanced"


def test_capacity_member_with_neither_never_appears() -> None:
    report = compute_capacity_report([MemberCapacityInput(member_id="eve", capacity_points=0)], [])
    assert report[0].member_id == "eve"
    assert report[0].assigned_points == 0
    assert report[0].status == "under"


def test_capacity_is_pure() -> None:
    capacities = [MemberCapacityInput(member_id="a", capacity_points=5)]
    assignments = [MemberAssignment(member_id="a", points=5)]
    assert compute_capacity_report(capacities, assignments) == compute_capacity_report(
        capacities, assignments
    )


# --------------------------------------------------------------------------- #
# estimation scales                                                            #
# --------------------------------------------------------------------------- #


def test_empty_scale_allows_anything() -> None:
    scale = EstimationScale(name="unrestricted")
    assert is_valid_estimate(scale, 4.5) is True


def test_fibonacci_scale_validates_membership() -> None:
    scale = EstimationScale(name="fibonacci", values=[1, 2, 3, 5, 8, 13])
    assert is_valid_estimate(scale, 5) is True
    assert is_valid_estimate(scale, 4) is False


def test_nearest_scale_value_snaps() -> None:
    scale = EstimationScale(name="fibonacci", values=[1, 2, 3, 5, 8, 13])
    assert nearest_scale_value(scale, 4) == 3  # 4 is closer to 3 (1) than 5 (1)... tie -> min()
    assert nearest_scale_value(scale, 9) == 8


def test_nearest_scale_value_identity_when_empty() -> None:
    scale = EstimationScale(name="unrestricted")
    assert nearest_scale_value(scale, 4.5) == 4.5


# --------------------------------------------------------------------------- #
# portfolio: CFD, cycle/lead time, cross-project velocity                      #
# --------------------------------------------------------------------------- #


def test_cfd_counts_tasks_by_last_known_status() -> None:
    events = [
        TaskStatusEventInput(
            task_id="t1", to_status="in_progress", changed_at=datetime(2026, 6, 1, 9)
        ),
        TaskStatusEventInput(task_id="t1", to_status="done", changed_at=datetime(2026, 6, 3, 9)),
        TaskStatusEventInput(
            task_id="t2", to_status="in_progress", changed_at=datetime(2026, 6, 2, 9)
        ),
    ]
    points = compute_cfd(events, date(2026, 6, 1), date(2026, 6, 3), initial_status="backlog")
    by_day = {p.snapshot_date: p.status_counts for p in points}
    assert by_day[date(2026, 6, 1)] == {"in_progress": 1, "backlog": 1}
    assert by_day[date(2026, 6, 2)] == {"in_progress": 2}
    assert by_day[date(2026, 6, 3)] == {"done": 1, "in_progress": 1}


def test_cfd_empty_events_is_empty_per_day() -> None:
    points = compute_cfd([], date(2026, 6, 1), date(2026, 6, 1))
    assert points[0].status_counts == {}


def test_cycle_and_lead_time_computed_from_transitions() -> None:
    events_by_task = {
        "t1": [
            TaskStatusEventInput(
                task_id="t1", to_status="in_progress", changed_at=datetime(2026, 6, 2)
            ),
            TaskStatusEventInput(task_id="t1", to_status="done", changed_at=datetime(2026, 6, 6)),
        ]
    }
    created_at_by_task = {"t1": datetime(2026, 6, 1)}
    rows = compute_cycle_lead_time(events_by_task, created_at_by_task)
    assert rows[0].lead_time_days == 5.0  # created 6/1 -> done 6/6
    assert rows[0].cycle_time_days == 4.0  # in_progress 6/2 -> done 6/6


def test_cycle_lead_time_none_when_not_done() -> None:
    events_by_task = {
        "t1": [
            TaskStatusEventInput(
                task_id="t1", to_status="in_progress", changed_at=datetime(2026, 6, 2)
            )
        ]
    }
    rows = compute_cycle_lead_time(events_by_task, {"t1": datetime(2026, 6, 1)})
    assert rows[0].lead_time_days is None
    assert rows[0].cycle_time_days is None


def test_average_cycle_lead_time_ignores_none_and_empty_is_zero() -> None:
    rows = compute_cycle_lead_time(
        {
            "t1": [
                TaskStatusEventInput(
                    task_id="t1", to_status="done", changed_at=datetime(2026, 6, 3)
                )
            ],
            "t2": [
                TaskStatusEventInput(
                    task_id="t2", to_status="in_progress", changed_at=datetime(2026, 6, 2)
                )
            ],
        },
        {"t1": datetime(2026, 6, 1), "t2": datetime(2026, 6, 1)},
    )
    avg_lead, avg_cycle = average_cycle_lead_time(rows)
    assert avg_lead == 2.0  # only t1 has a value
    assert avg_cycle == 0.0  # neither has a cycle time (no in_progress->done pair for t1)
    assert average_cycle_lead_time([]) == (0.0, 0.0)


def test_portfolio_velocity_weights_predictability_by_sprint_count() -> None:
    per_project = {
        "p1": VelocitySummary(sprint_count=2, average_velocity=10, predictability_avg=1.0),
        "p2": VelocitySummary(sprint_count=1, average_velocity=20, predictability_avg=0.5),
    }
    summary = compute_portfolio_velocity(per_project)
    assert summary.project_count == 2
    assert summary.total_average_velocity == 30.0
    # weighted: (1.0*2 + 0.5*1) / 3 = 0.8333
    assert summary.weighted_predictability == round(2.5 / 3, 4)


def test_portfolio_velocity_empty_is_zeros() -> None:
    summary = compute_portfolio_velocity({})
    assert summary.project_count == 0
    assert summary.weighted_predictability == 0.0


# --------------------------------------------------------------------------- #
# sprint-goal <-> acceptance-criteria alignment                                #
# --------------------------------------------------------------------------- #


def test_goal_alignment_flags_tasks_sharing_no_tokens() -> None:
    tasks = [
        TaskAlignmentInput(task_id="t1", title="Redesign the checkout flow"),
        TaskAlignmentInput(task_id="t2", title="Fix a typo in the footer"),
    ]
    result = compute_goal_alignment("Ship the checkout redesign", tasks)
    assert result.total_count == 2
    assert result.aligned_count == 1
    assert result.unaligned_task_ids == ["t2"]
    assert result.alignment_ratio == 0.5


def test_goal_alignment_acceptance_criteria_also_scored() -> None:
    tasks = [
        TaskAlignmentInput(
            task_id="t1", title="Backend work", acceptance_criteria=["Supports checkout flow"]
        )
    ]
    result = compute_goal_alignment("checkout redesign", tasks)
    assert result.aligned_count == 1


def test_goal_alignment_no_goal_is_neutral() -> None:
    tasks = [TaskAlignmentInput(task_id="t1", title="Anything")]
    result = compute_goal_alignment(None, tasks)
    assert result.total_count == 1
    assert result.aligned_count == 0
    assert result.unaligned_task_ids == []


def test_goal_alignment_only_stopwords_is_neutral() -> None:
    tasks = [TaskAlignmentInput(task_id="t1", title="Anything")]
    result = compute_goal_alignment("the and for", tasks)
    assert result.unaligned_task_ids == []
