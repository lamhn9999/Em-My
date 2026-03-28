"""
core/scheduler.py
──────────────────────────────────────────────────────────────────────────────
Flexible Job Shop Scheduling engine.

Each customer booking is a "job" whose service has N sequential "operations"
(steps). Each operation requires a specific resource type (staff with a skill,
or a station). The scheduler finds the earliest valid assignment of resources
to steps, respecting:
  • Resource non-overlap  (no two jobs share a resource at the same time)
  • Operation sequence    (step i must finish before step i+1 starts)
  • Unattended wait steps (no resource needed, just elapsed time)
  • Preferred staff       (honoured if available, otherwise best match)
  • Staff working hours   (no assignment outside declared hours)

Strategy
────────
Primary:  OR-Tools CP-SAT solver  (imported lazily so the app starts even
          if ortools is not installed)
Fallback: Greedy left-most-fit scheduler used when ortools is absent or
          when CP-SAT cannot solve within the time limit.

Public API
──────────
  schedule(request, db, preferred_start_minutes) -> ScheduleResult
  find_alternatives(request, db, count, granularity) -> list[ScheduleResult]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from data.models import (
    ResourceAssignment,
    ServiceDefinition,
    ServiceStep,
    Staff,
    Station,
)

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ScheduleRequest:
    """Everything the scheduler needs to build a schedule."""
    date:            str                    # YYYY-MM-DD
    service:         ServiceDefinition
    preferred_time:  str                    # HH:MM — customer's requested start
    all_staff:       list[Staff]
    all_stations:    list[Station]
    existing_assignments: list[dict]        # from db.get_resource_assignments_for_date
    preferred_staff_id: Optional[str] = None   # honour if possible


@dataclass
class ScheduleResult:
    success:               bool
    start_time:            str                     # HH:MM of first active step
    end_time:              str                     # HH:MM of last step end
    total_duration:        int                     # minutes
    steps:                 list[ResourceAssignment] = field(default_factory=list)
    failure_reason:        str = ""
    staff_warning:         str = ""               # non-empty when preferred staff was substituted due to skill mismatch
    suggested_retry_after: str = ""               # HH:MM when preferred staff is next free (set on schedule-conflict failure)


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _hhmm_to_min(t: str) -> int:
    h, m = map(int, t.split(":"))
    return h * 60 + m


def _min_to_hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _day_of_week(date_str: str) -> str:
    """Returns 'mon', 'tue', … for a YYYY-MM-DD string."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][dt.weekday()]


def _staff_available_range(staff: Staff, day: str) -> tuple[int, int] | None:
    """Returns (open_min, close_min) or None if staff doesn't work that day."""
    hours = staff.working_hours.get(day)
    if not hours or len(hours) < 2:
        return None
    return _hhmm_to_min(hours[0]), _hhmm_to_min(hours[1])


def _build_busy_map(existing: list[dict]) -> dict[str, list[tuple[int, int]]]:
    """
    Returns {resource_id: [(start_min, end_min), …]} sorted by start.
    """
    busy: dict[str, list[tuple[int, int]]] = {}
    for a in existing:
        rid = a["resource_id"]
        s = _hhmm_to_min(a["start_time"])
        e = _hhmm_to_min(a["end_time"])
        busy.setdefault(rid, []).append((s, e))
    for v in busy.values():
        v.sort()
    return busy


def _is_free(
    resource_id: str,
    start: int,
    end: int,
    busy: dict[str, list[tuple[int, int]]],
) -> bool:
    for s, e in busy.get(resource_id, []):
        if max(start, s) < min(end, e):
            return False
    return True


def _mark_busy(
    resource_id: str,
    start: int,
    end: int,
    busy: dict[str, list[tuple[int, int]]],
) -> None:
    busy.setdefault(resource_id, []).append((start, end))
    busy[resource_id].sort()


def _preferred_busy_until(req: ScheduleRequest, from_min: int) -> str:
    """Return the earliest HH:MM after from_min when the preferred staff is free."""
    busy = _build_busy_map(req.existing_assignments)
    free_after = from_min
    for s, e in sorted(busy.get(req.preferred_staff_id or "", [])):
        if max(from_min, s) < e:
            free_after = max(free_after, e)
    return _min_to_hhmm(free_after)


def _candidates_for_step(
    step: ServiceStep,
    all_staff: list[Staff],
    all_stations: list[Station],
    preferred_staff_id: Optional[str],
    day: str,
) -> tuple[list[tuple[str, str, Optional[str], Optional[str]]], bool]:
    """
    Returns (candidates, preferred_was_skipped).

    Each candidate is a 4-tuple:
      (primary_resource_id, primary_resource_type, paired_staff_id, paired_staff_type)

    • Stylist step:          (staff_id, "stylist", None, None)
    • Station-only step:     (station_id, station_type, None, None)
    • Station + staff step:  (station_id, station_type, staff_id, "stylist")
      — when step.staff_skill_required is set, the station must be operated by
        a qualified staff member simultaneously (e.g. washing_bed needs a "wash"-
        skilled person). Both resources are booked for the same time window.

    Preferred-staff enforcement (stylist steps only):
      If preferred_staff_id is set and that person has the skill for this step,
      they are returned as the SOLE candidate — this guarantees their assignment
      and causes the schedule to fail (→ negotiation) if they are busy.
      Station + staff steps never restrict candidates by preference; the dedicated
      technician (e.g. Tuấn for wash/rinse) is always selected on skill alone.

    preferred_was_skipped=True means the preferred staff exists but lacks the
    skill for a STYLIST step (triggers a warning to the customer).
    """
    if step.resource_type is None:
        return [], False  # wait step — no resource

    candidates: list[tuple[str, str, Optional[str], Optional[str]]] = []
    preferred_skipped = False

    if step.resource_type == "stylist":
        skill = step.skill_required or ""
        # If preferred staff can perform this stylist step, enforce them exclusively.
        # Working-hours and busy-slot checks happen in the scheduler itself; if they
        # are unavailable the schedule fails and the negotiation agent takes over.
        if preferred_staff_id:
            pref_obj = next((s for s in all_staff if s.id == preferred_staff_id), None)
            if pref_obj and (not skill or skill in pref_obj.skills):
                return [(pref_obj.id, "stylist", None, None)], False
            # Preferred staff can't do this step — warn and open to all
            if pref_obj:
                preferred_skipped = True

        matched = [
            s for s in all_staff
            if (not skill or skill in s.skills)
            and _staff_available_range(s, day) is not None
        ]
        matched.sort(key=lambda s: (0 if s.id == preferred_staff_id else 1))
        candidates = [(s.id, "stylist", None, None) for s in matched]

    elif step.staff_skill_required:
        # Station step that also requires a human operator with the given skill.
        # Preference does NOT apply here — the dedicated technician (e.g. Tuấn)
        # is always selected purely on skill. Never set pref_skipped for these.
        skill = step.staff_skill_required
        matched_stations = [st for st in all_stations if st.type == step.resource_type]
        matched_staff = [
            s for s in all_staff
            if skill in s.skills
            and _staff_available_range(s, day) is not None
        ]
        # Cartesian product: each station paired with each qualified staff member
        candidates = [
            (st.id, st.type, s.id, "stylist")
            for st in matched_stations
            for s in matched_staff
        ]

    else:
        matched_stations = [
            st for st in all_stations if st.type == step.resource_type
        ]
        candidates = [(st.id, st.type, None, None) for st in matched_stations]

    return candidates, preferred_skipped


# ──────────────────────────────────────────────────────────────────────────────
# Greedy scheduler
# ──────────────────────────────────────────────────────────────────────────────

def _greedy_schedule(req: ScheduleRequest, start_min: int) -> ScheduleResult:
    """
    Left-most-fit greedy scheduler (fallback when CP-SAT is unavailable).
    Assigns each step to the earliest-free resource that satisfies constraints.
    Steps must execute in sequence (step i+1 starts after step i ends).
    """
    log.info(
        "[Greedy] Scheduling '%s' on %s from %s | %d existing assignments",
        req.service.name, req.date, _min_to_hhmm(start_min), len(req.existing_assignments),
    )
    busy = _build_busy_map(req.existing_assignments)
    day  = _day_of_week(req.date)
    steps_out: list[ResourceAssignment] = []
    staff_warning = ""

    cursor = start_min

    for step in req.service.steps:
        step_end = cursor + step.duration

        # Wait step — no resource, just advance the cursor
        if step.resource_type is None:
            steps_out.append(
                ResourceAssignment(
                    step_index=step.step_index,
                    step_type=step.step_type,
                    resource_id="__wait__",
                    resource_type="wait",
                    start_time=_min_to_hhmm(cursor),
                    end_time=_min_to_hhmm(step_end),
                    duration=step.duration,
                )
            )
            cursor = step_end
            continue

        candidates, pref_skipped = _candidates_for_step(
            step, req.all_staff, req.all_stations, req.preferred_staff_id, day
        )
        if pref_skipped and not staff_warning:
            pref_obj = next((s for s in req.all_staff if s.id == req.preferred_staff_id), None)
            skill = step.skill_required or step.staff_skill_required or step.step_type
            staff_warning = (
                f"{pref_obj.name if pref_obj else req.preferred_staff_id} không thực hiện "
                f"được dịch vụ này (thiếu kỹ năng '{skill}'). "
                "Đã sắp xếp nhân viên phù hợp thay thế."
            )
            log.info("[Greedy] Preferred staff skipped for step '%s': %s", step.step_type, staff_warning)

        if not candidates:
            return ScheduleResult(
                success=False, start_time="", end_time="", total_duration=0,
                failure_reason=f"Không có nhân viên / trạm phù hợp cho bước '{step.step_type}'.",
            )

        assigned = False
        best_start = cursor
        best_rid: str = ""
        best_rtype: str = ""
        best_staff_id: Optional[str] = None
        best_end: int = cursor

        for rid, rtype, staff_id, _staff_type in candidates:
            # Determine working-hours window.
            # For stylist steps: the staff's own hours.
            # For station+staff steps: the operator's hours constrain the slot.
            # For plain station steps: no restriction (24 h).
            if rtype == "stylist":
                avail = _staff_available_range(
                    next(s for s in req.all_staff if s.id == rid), day
                )
                if avail is None:
                    continue
                open_min, close_min = avail
            elif staff_id:
                staff_obj = next((s for s in req.all_staff if s.id == staff_id), None)
                avail = _staff_available_range(staff_obj, day) if staff_obj else None
                if avail is None:
                    continue
                open_min, close_min = avail
            else:
                open_min, close_min = 0, 24 * 60

            slot_start = max(cursor, open_min)
            slot_end   = slot_start + step.duration

            # Advance past all conflicts for both resources (iterate until stable).
            changed = True
            while changed and slot_end <= close_min:
                changed = False
                for busy_s, busy_e in busy.get(rid, []):
                    if max(slot_start, busy_s) < min(slot_end, busy_e):
                        slot_start = busy_e
                        slot_end   = slot_start + step.duration
                        changed    = True
                        break
                if staff_id and not changed:
                    for busy_s, busy_e in busy.get(staff_id, []):
                        if max(slot_start, busy_s) < min(slot_end, busy_e):
                            slot_start = busy_e
                            slot_end   = slot_start + step.duration
                            changed    = True
                            break

            if slot_end > close_min:
                continue

            if not assigned or slot_start < best_start:
                best_rid      = rid
                best_rtype    = rtype
                best_staff_id = staff_id
                best_start    = slot_start
                best_end      = slot_end
                assigned      = True

        if not assigned:
            return ScheduleResult(
                success=False, start_time="", end_time="", total_duration=0,
                failure_reason=f"Không có khung giờ trống cho bước '{step.step_type}' vào ngày {req.date}.",
            )

        log.info(
            "[Greedy]  step %-12s → %-15s%s %s–%s",
            step.step_type, best_rid,
            f" + {best_staff_id}" if best_staff_id else "",
            _min_to_hhmm(best_start), _min_to_hhmm(best_end),
        )
        _mark_busy(best_rid, best_start, best_end, busy)
        steps_out.append(
            ResourceAssignment(
                step_index=step.step_index,
                step_type=step.step_type,
                resource_id=best_rid,
                resource_type=best_rtype,
                start_time=_min_to_hhmm(best_start),
                end_time=_min_to_hhmm(best_end),
                duration=step.duration,
            )
        )
        # If this step also requires a paired staff operator, emit a second assignment
        # and block that staff member's time too.
        if best_staff_id:
            _mark_busy(best_staff_id, best_start, best_end, busy)
            steps_out.append(
                ResourceAssignment(
                    step_index=step.step_index,
                    step_type=step.step_type,
                    resource_id=best_staff_id,
                    resource_type="stylist",
                    start_time=_min_to_hhmm(best_start),
                    end_time=_min_to_hhmm(best_end),
                    duration=step.duration,
                )
            )
        cursor = best_end

    overall_start = steps_out[0].start_time if steps_out else _min_to_hhmm(start_min)
    overall_end   = steps_out[-1].end_time  if steps_out else _min_to_hhmm(start_min)
    # Deduplicate step_index when paired steps produce two ResourceAssignment entries
    seen_step_indices: set[int] = set()
    total_dur = 0
    for s in steps_out:
        if s.step_index not in seen_step_indices:
            seen_step_indices.add(s.step_index)
            total_dur += s.duration

    log.info("[Greedy] ✅ Scheduled %s → %s (~%dmin)", overall_start, overall_end, total_dur)
    return ScheduleResult(
        success=True,
        start_time=overall_start,
        end_time=overall_end,
        total_duration=total_dur,
        steps=steps_out,
        staff_warning=staff_warning,
    )


# ──────────────────────────────────────────────────────────────────────────────
# CP-SAT scheduler (OR-Tools)
# ──────────────────────────────────────────────────────────────────────────────

def _cpsat_schedule(
    req: ScheduleRequest,
    start_min: int,
    exclude_start_before: int = 0,
) -> ScheduleResult:
    """
    CP-SAT Flexible Job Shop scheduler.

    Variables:
      For each (step, candidate_resource): bool presence var
      For each step: integer start var (≥ start_min)

    Constraints:
      • Exactly one resource per step
      • Step sequence: start[i+1] >= end[i]
      • No two steps share a resource at the same time
      • Resource must be free (existing_assignments respected as hard blocks)
      • exclude_start_before: first active step must start ≥ this value
        (used by find_alternatives to enumerate distinct solutions)

    Objective: minimise makespan (last step end time)
    """
    try:
        from ortools.sat.python import cp_model  # type: ignore
    except ImportError:
        log.warning("[CP-SAT] ortools not installed — falling back to greedy")
        return _greedy_schedule(req, start_min)

    log.info(
        "[CP-SAT] Solving '%s' on %s from %s | existing=%d | exclude_before=%s",
        req.service.name, req.date, _min_to_hhmm(start_min),
        len(req.existing_assignments),
        _min_to_hhmm(exclude_start_before) if exclude_start_before else "none",
    )

    day  = _day_of_week(req.date)
    busy = _build_busy_map(req.existing_assignments)
    staff_warning = ""

    model   = cp_model.CpModel()
    horizon = 24 * 60

    # ── Build variables ───────────────────────────────────────────────────────
    step_vars = []

    for step in req.service.steps:
        dur       = step.duration
        start_var = model.NewIntVar(start_min, horizon - dur, f"start_{step.step_index}")
        end_var   = model.NewIntVar(start_min + dur, horizon,  f"end_{step.step_index}")
        model.Add(end_var == start_var + dur)

        if step.resource_type is None:
            step_vars.append((start_var, end_var, []))
            continue

        candidates, pref_skipped = _candidates_for_step(
            step, req.all_staff, req.all_stations, req.preferred_staff_id, day
        )
        if pref_skipped and not staff_warning:
            pref_obj = next((s for s in req.all_staff if s.id == req.preferred_staff_id), None)
            skill = step.skill_required or step.staff_skill_required or step.step_type
            staff_warning = (
                f"{pref_obj.name if pref_obj else req.preferred_staff_id} không thực hiện "
                f"được dịch vụ này (thiếu kỹ năng '{skill}'). "
                "Đã sắp xếp nhân viên phù hợp thay thế."
            )
            log.info("[CP-SAT] Preferred staff skipped for step '%s': %s", step.step_type, staff_warning)

        if not candidates:
            return ScheduleResult(
                success=False, start_time="", end_time="", total_duration=0,
                failure_reason=f"Không có nhân viên / trạm phù hợp cho bước '{step.step_type}'.",
            )

        # option_intervals: list of (rid, rtype, staff_id|None, iv, presence)
        # For paired station+staff candidates, one presence bool covers BOTH resources —
        # the same interval var is registered under both resource_ids in resource_intervals.
        option_intervals = []
        for rid, rtype, staff_id, _staff_type in candidates:
            presence = model.NewBoolVar(
                f"p_{step.step_index}_{rid}" + (f"_{staff_id}" if staff_id else "")
            )
            iv = model.NewOptionalIntervalVar(
                start_var, dur, end_var, presence,
                f"iv_{step.step_index}_{rid}",
            )
            # Apply working-hours constraints
            if rtype == "stylist":
                avail = _staff_available_range(
                    next(s for s in req.all_staff if s.id == rid), day
                )
                if avail is None:
                    model.Add(presence == 0)
                else:
                    open_m, close_m = avail
                    model.Add(start_var >= open_m).OnlyEnforceIf(presence)
                    model.Add(end_var   <= close_m).OnlyEnforceIf(presence)
            elif staff_id:
                # Station + paired staff: constrain by the operator's working hours
                staff_obj = next((s for s in req.all_staff if s.id == staff_id), None)
                avail = _staff_available_range(staff_obj, day) if staff_obj else None
                if avail is None:
                    model.Add(presence == 0)
                else:
                    open_m, close_m = avail
                    model.Add(start_var >= open_m).OnlyEnforceIf(presence)
                    model.Add(end_var   <= close_m).OnlyEnforceIf(presence)
            option_intervals.append((rid, rtype, staff_id, iv, presence))

        model.AddExactlyOne([p for _, _, _, _, p in option_intervals])
        step_vars.append((start_var, end_var, option_intervals))

    # ── Sequence constraints ──────────────────────────────────────────────────
    for i in range(len(req.service.steps) - 1):
        _, e_var, _  = step_vars[i]
        ns_var, _, _ = step_vars[i + 1]
        model.Add(ns_var >= e_var)

    # ── Exclude-before constraint (for find_alternatives enumeration) ─────────
    if exclude_start_before > 0 and step_vars:
        first_sv, _, _ = step_vars[0]
        model.Add(first_sv >= exclude_start_before)

    # ── Resource non-overlap (new-job steps vs each other) ───────────────────
    # For paired station+staff candidates, register the same interval under
    # both resource IDs so that AddNoOverlap blocks each resource independently.
    resource_intervals: dict[str, list] = {}
    for _, _, options in step_vars:
        for rid, _, staff_id, iv, _ in options:
            resource_intervals.setdefault(rid, []).append(iv)
            if staff_id:
                resource_intervals.setdefault(staff_id, []).append(iv)

    for rid, ivs in resource_intervals.items():
        if len(ivs) > 1:
            model.AddNoOverlap(ivs)

    # ── Block existing confirmed assignments ──────────────────────────────────
    for rid, intervals_list in resource_intervals.items():
        for busy_s, busy_e in busy.get(rid, []):
            b_iv = model.NewIntervalVar(
                model.NewConstant(busy_s),
                busy_e - busy_s,
                model.NewConstant(busy_e),
                f"block_{rid}_{busy_s}_{busy_e}",
            )
            model.AddNoOverlap(intervals_list + [b_iv])

    # ── Hints ─────────────────────────────────────────────────────────────────
    offset = max(start_min, exclude_start_before)
    for i, (sv, _, _) in enumerate(step_vars):
        model.AddHint(sv, offset + sum(req.service.steps[j].duration for j in range(i)))

    if req.preferred_staff_id:
        for _, _, options in step_vars:
            for rid, _, staff_id, _, presence in options:
                is_pref = (rid == req.preferred_staff_id or staff_id == req.preferred_staff_id)
                model.AddHint(presence, 1 if is_pref else 0)

    # ── Objective: minimise makespan ──────────────────────────────────────────
    makespan = model.NewIntVar(0, horizon, "makespan")
    if step_vars:
        model.AddMaxEquality(makespan, [e for _, e, _ in step_vars])
    model.Minimize(makespan)

    # ── Solve ─────────────────────────────────────────────────────────────────
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5.0
    solver.parameters.num_search_workers  = 2
    status = solver.Solve(model)

    status_name = solver.StatusName(status)
    log.info(
        "[CP-SAT] Status=%s | WallTime=%.3fs | Objective=%s",
        status_name,
        solver.WallTime(),
        solver.ObjectiveValue() if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else "N/A",
    )

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return ScheduleResult(
            success=False, start_time="", end_time="", total_duration=0,
            failure_reason=(
                f"Không tìm được lịch khả thi cho ngày {req.date} "
                f"từ {_min_to_hhmm(start_min)} trở đi."
            ),
        )

    # ── Extract results ───────────────────────────────────────────────────────
    steps_out: list[ResourceAssignment] = []
    for step, (sv, ev, options) in zip(req.service.steps, step_vars):
        s = solver.Value(sv)
        e = solver.Value(ev)

        if step.resource_type is None:
            rid, rtype, chosen_staff_id = "__wait__", "wait", None
        else:
            rid, rtype, chosen_staff_id = "__unknown__", step.resource_type, None
            for r_id, r_type, staff_id, _, presence in options:
                if solver.Value(presence):
                    rid, rtype, chosen_staff_id = r_id, r_type, staff_id
                    break

        log.info(
            "[CP-SAT]  step %-12s → %-15s%s %s–%s",
            step.step_type, rid,
            f" + {chosen_staff_id}" if chosen_staff_id else "",
            _min_to_hhmm(s), _min_to_hhmm(e),
        )
        steps_out.append(
            ResourceAssignment(
                step_index=step.step_index,
                step_type=step.step_type,
                resource_id=rid,
                resource_type=rtype,
                start_time=_min_to_hhmm(s),
                end_time=_min_to_hhmm(e),
                duration=step.duration,
            )
        )
        # For paired station+staff steps, emit a second assignment for the operator
        if chosen_staff_id:
            steps_out.append(
                ResourceAssignment(
                    step_index=step.step_index,
                    step_type=step.step_type,
                    resource_id=chosen_staff_id,
                    resource_type="stylist",
                    start_time=_min_to_hhmm(s),
                    end_time=_min_to_hhmm(e),
                    duration=step.duration,
                )
            )

    overall_start = steps_out[0].start_time if steps_out else _min_to_hhmm(start_min)
    overall_end   = steps_out[-1].end_time  if steps_out else _min_to_hhmm(start_min)
    # Deduplicate step_index when paired steps produce two ResourceAssignment entries
    seen_step_indices: set[int] = set()
    total_dur = 0
    for s in steps_out:
        if s.step_index not in seen_step_indices:
            seen_step_indices.add(s.step_index)
            total_dur += s.duration

    log.info("[CP-SAT] ✅ Scheduled %s → %s (~%dmin)", overall_start, overall_end, total_dur)
    return ScheduleResult(
        success=True,
        start_time=overall_start,
        end_time=overall_end,
        total_duration=total_dur,
        steps=steps_out,
        staff_warning=staff_warning,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def _pref_can_do_all_steps(req: ScheduleRequest) -> bool:
    """
    True when the preferred staff has the required skill for every STYLIST step.

    Station + staff steps (washing_bed etc.) are intentionally excluded: those
    are always handled by a dedicated technician (e.g. Tuấn for wash/rinse)
    regardless of which stylist the customer prefers.  Checking them here would
    wrongly disqualify a preferred stylist like Minh for "cắt + gội" just
    because he can't operate the wash station.
    """
    if not req.preferred_staff_id:
        return False
    pref_obj = next((s for s in req.all_staff if s.id == req.preferred_staff_id), None)
    if pref_obj is None:
        return False
    return all(
        step.resource_type != "stylist"
        or not step.skill_required
        or step.skill_required in pref_obj.skills
        for step in req.service.steps
    )


def _strict_req(req: ScheduleRequest) -> ScheduleRequest:
    """Return a copy of *req* with all_staff restricted to the preferred staff only."""
    pref_obj = next(s for s in req.all_staff if s.id == req.preferred_staff_id)
    return ScheduleRequest(
        date=req.date,
        service=req.service,
        preferred_time=req.preferred_time,
        all_staff=[pref_obj],
        all_stations=req.all_stations,
        existing_assignments=req.existing_assignments,
        preferred_staff_id=req.preferred_staff_id,
    )


async def schedule(req: ScheduleRequest, use_cpsat: bool = True) -> ScheduleResult:
    """
    Find the best schedule for the given request starting as close as possible
    to req.preferred_time.

    Strict preferred-staff rule: when the customer named a specific staff member
    who can do all required steps, the scheduler runs with ONLY that staff in the
    candidate pool.  This is deterministic — success means they were assigned,
    failure means they are genuinely unavailable at the requested time.
    The negotiation agent is triggered on failure.
    """
    start_min = _hhmm_to_min(req.preferred_time)

    # ── Preferred-staff mode ──────────────────────────────────────────────────
    # When the preferred stylist can perform all stylist steps, _candidates_for_step
    # already returns them as the sole candidate for those steps (per-step enforcement).
    # Station + staff steps (wash/rinse) are always assigned to the dedicated
    # technician regardless of preference.  If the preferred stylist is busy the
    # scheduler returns INFEASIBLE → we surface a negotiation failure here.
    if _pref_can_do_all_steps(req):
        if use_cpsat:
            result = _cpsat_schedule(req, start_min)
        else:
            result = _greedy_schedule(req, start_min)

        if result.success:
            pref_obj = next(s for s in req.all_staff if s.id == req.preferred_staff_id)
            log.info("[Schedule] ✅ Preferred staff %s assigned", pref_obj.name)
            return result

        # Preferred staff truly unavailable → trigger negotiation
        pref_obj = next(s for s in req.all_staff if s.id == req.preferred_staff_id)
        retry    = _preferred_busy_until(req, start_min)
        log.info(
            "[Schedule] ⛔ Preferred staff %s unavailable at %s → negotiate; retry after %s",
            pref_obj.name, req.preferred_time, retry,
        )
        return ScheduleResult(
            success=False,
            start_time="", end_time="", total_duration=0,
            failure_reason=(
                f"{pref_obj.name} đã có lịch vào {req.preferred_time} ngày {req.date}. "
                f"Bạn muốn đặt giờ khác với {pref_obj.name}, hay chọn nhân viên khác ạ?"
            ),
            suggested_retry_after=retry,
        )

    # ── No preferred staff (or they lack skills for some stylist step) ────────
    if use_cpsat:
        result = _cpsat_schedule(req, start_min)
    else:
        result = _greedy_schedule(req, start_min)
    return result


async def find_alternatives(
    req: ScheduleRequest,
    count: int = 3,
    granularity: int = 30,
    use_cpsat: bool = True,
) -> list[ScheduleResult]:
    """
    Return up to `count` valid alternative time slots starting at or after the
    preferred time, each at least `granularity` minutes apart.

    CP-SAT strategy:
      Because CP-SAT always minimises makespan it returns the globally earliest
      feasible schedule. To enumerate distinct alternatives we use the
      `exclude_start_before` parameter: after finding result R_i we force the
      next solve to start at R_i.start_time + granularity, guaranteeing each
      returned slot is strictly later than the previous one.

    Greedy strategy:
      We iterate over candidate start times in order; each greedy call can only
      start at or after the given start_min, so the same enumeration works.
    """
    from config.business import BUSINESS_HOURS

    day = _day_of_week(req.date)
    biz = BUSINESS_HOURS.get(day)
    if not biz:
        return []

    open_min  = _hhmm_to_min(biz[0])
    close_min = _hhmm_to_min(biz[1])
    preferred = _hhmm_to_min(req.preferred_time)

    results: list[ScheduleResult] = []
    # First search window starts at preferred time; each iteration starts after
    # the previous result's start + granularity.
    next_start = preferred

    log.info(
        "[Alternatives] Looking for %d slots for '%s' on %s from %s",
        count, req.service.name, req.date, _min_to_hhmm(preferred),
    )

    # _candidates_for_step enforces preferred staff per stylist step;
    # no need for a separate restricted request.
    use_strict = _pref_can_do_all_steps(req)
    active_req = req
    pref_obj   = (
        next((s for s in req.all_staff if s.id == req.preferred_staff_id), None)
        if req.preferred_staff_id else None
    )

    _max_iters = (close_min - open_min) // max(granularity, 1) + 1
    _iters     = 0

    while len(results) < count:
        _iters += 1
        if _iters > _max_iters:
            log.info("[Alternatives] Iteration cap reached — stopping")
            break

        if next_start + req.service.total_duration > close_min:
            break  # no room left in the day

        if use_cpsat:
            result = _cpsat_schedule(active_req, open_min, exclude_start_before=next_start)
        else:
            trial_req = ScheduleRequest(
                date=active_req.date,
                service=active_req.service,
                preferred_time=_min_to_hhmm(next_start),
                all_staff=active_req.all_staff,
                all_stations=active_req.all_stations,
                existing_assignments=active_req.existing_assignments,
                preferred_staff_id=active_req.preferred_staff_id,
            )
            result = _greedy_schedule(trial_req, next_start)

        if not result.success:
            if use_strict and pref_obj:
                # Preferred staff blocked in this window — find when they're free
                retry_str = _preferred_busy_until(req, next_start)
                retry_min = _hhmm_to_min(retry_str)
                next_start = max(retry_min, next_start + granularity)
                log.info(
                    "[Alternatives] %s blocked; skipping to %s",
                    pref_obj.name, _min_to_hhmm(next_start),
                )
                continue
            break  # no more feasible slots (non-strict mode)

        results.append(result)
        log.info(
            "[Alternatives] Found slot %d/%d: %s–%s (%s)",
            len(results), count, result.start_time, result.end_time,
            result.steps[0].resource_id if result.steps else "?",
        )
        next_start = _hhmm_to_min(result.start_time) + granularity

    return results


def build_schedule_request(
    date: str,
    preferred_time: str,
    service: ServiceDefinition,
    all_staff: list[Staff],
    all_stations: list[Station],
    existing_assignments: list[dict],
    preferred_staff_id: Optional[str] = None,
) -> ScheduleRequest:
    """Convenience constructor."""
    return ScheduleRequest(
        date=date,
        service=service,
        preferred_time=preferred_time,
        all_staff=all_staff,
        all_stations=all_stations,
        existing_assignments=existing_assignments,
        preferred_staff_id=preferred_staff_id,
    )
