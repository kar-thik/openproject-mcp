"""MCP prompts: report and workflow templates (SPEC §10).

Prompts surface as slash commands in Claude Code. They render structured data
(from the reporting collectors) into ready-to-send documents, parameterized by
``locale`` so localized templates are configuration, not forks.

Four prompts land here:

=========================  ==================================================
Prompt                     Renders
=========================  ==================================================
``weekly_report``          the 8-section (A-H) Agile/Scrum weekly report
``daily_standup``          yesterday's movement, due today, blockers
``triage_inbox``           unread notifications grouped by reason
``groom_backlog``          unassigned / unestimated / stale open work
=========================  ==================================================

Non-negotiables for this module:

* **The data is fetched here, server-side.** FastMCP enters a request context
  around ``prompts/get``, so ``_shared.get_tool_context()`` reaches the same
  lifespan client the tools use. A prompt therefore returns a *rendered*
  document, not instructions telling the model to go and call tools — the model
  gets the numbers with the template, in one round trip.
* **Done comes from ``isClosed``**, never from status names:
  ``ReportWorkPackage.is_closed`` carries the instance's own flag, and a row
  whose flag is unknown is never silently called open (SPEC §6.14). Among the
  open rows, *Planned* is the ones whose last change is still the day they were
  raised — not "absent from the ``updatedAt`` window", which is structurally
  empty because a row created inside a window is always changed inside it too.
* **The document repeats the collector's ``notes``.** A capped list or an
  unreadable roster is printed in the report, so nobody quotes a partial number
  as a total (G1/G5).
* **Upstream failures are data, not exceptions** (G4): a failed read renders a
  short document containing the structured error envelope and what to do about
  it, so the conversation continues instead of dying in a protocol error.
* The Vietnamese variant preserves the section structure of the template this
  server replaces (A. THÔNG TIN CHUNG … H. SPRINT HEALTH & CẢI TIẾN, plus the
  executive appendix). English is the default rendering; an unknown locale falls
  back to English **and says so** rather than half-translating.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from openproject_mcp.client.errors import OpenProjectError
from openproject_mcp.projections import Ref
from openproject_mcp.tools import _shared, reporting
from openproject_mcp.tools.reporting import (
    BacklogItem,
    BacklogSweep,
    Impediment,
    InboxSummary,
    ProjectReportData,
    ReportWorkPackage,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["DEFAULT_LOCALE", "LOCALES", "register"]

#: The rendering used when ``locale`` is omitted or unknown.
DEFAULT_LOCALE = "en"

#: Days a weekly report covers when the caller gives no window.
WEEKLY_WINDOW_DAYS = 7

#: Rows printed per table before the rest is summarized; keeps a rendered report
#: readable without hiding anything (the counts above each table are complete).
TABLE_ROW_LIMIT = 40

#: Deliverables highlighted in the executive summary.
HIGHLIGHT_LIMIT = 3

#: Items listed as next week's top priorities.
PRIORITY_LIMIT = 5

MANUAL = "_(fill in manually)_"
MANUAL_VI = "_(cần cập nhật thủ công)_"

LABELS: dict[str, dict[str, str]] = {
    "en": {
        "title": "WEEKLY REPORT - AGILE SCRUM",
        "generated": "Generated from live OpenProject data",
        "a": "A. GENERAL INFORMATION",
        "b": "B. EXECUTIVE SUMMARY",
        "c": "C. DELIVERY & BACKLOG MOVEMENT",
        "d": "D. RESOURCES & DELIVERY CAPACITY",
        "e": "E. IMPEDIMENTS & DEPENDENCIES",
        "f": "F. QUALITY & SYSTEM STABILITY",
        "g": "G. PLAN FOR NEXT WEEK",
        "h": "H. SPRINT HEALTH & IMPROVEMENTS",
        "appendix": "APPENDIX: ONE-PAGER FOR LEADERSHIP",
        "field": "Field",
        "value": "Value",
        "window": "Reporting window",
        "team": "Team / Squad",
        "product": "Product / Module",
        "project_id": "Project id",
        "open_now": "Open work packages now",
        "progress": "Progress against the sprint goal",
        "on_track": "On track",
        "at_risk": "At risk",
        "off_track": "Off track",
        "top_deliverables": "Highlighted deliverables (done):",
        "none_done": "Nothing was completed in this window.",
        "biggest_blocker": "Biggest impediment",
        "no_blockers": "none found",
        "blocked_items": "blocking relation(s) on open work",
        "needs_decision": "Support / decisions needed",
        "sub_done": "1) Completed (Done)",
        "sub_in_progress": "2) In progress",
        "sub_planned": "3) Raised but not started (Planned)",
        "th_ticket": "Ticket",
        "th_summary": "Summary",
        "th_owner": "Owner",
        "th_date": "Due / last change",
        "th_status": "Status",
        "none_in_progress": "Nothing is in progress.",
        "none_planned": "Nothing is waiting to start.",
        "team_size": "Team size",
        "capacity": "Capacity logged in the window",
        "people": "people",
        "hours": "hours",
        "by_activity": "Effort by activity:",
        "by_person": "Effort by person:",
        "th_activity": "Activity",
        "th_hours": "Hours",
        "th_share": "Share",
        "th_person": "Person",
        "th_entries": "Entries",
        "entries_scope": "{scanned} of {total} time entries",
        "no_time": "No time was logged in this window (or it is not visible to this account).",
        "th_item": "Work package",
        "th_relation": "Relation",
        "th_note": "Note",
        "no_impediments": "No blocking relations were found on the open work packages scanned.",
        "created_by_type": "Work packages created in the window, by type:",
        "th_type": "Type",
        "th_count": "Count",
        "open_by_status": "Open work packages by status (server-side counts):",
        "no_created": "Nothing was created in this window.",
        "next_priorities": "Top priorities:",
        "none_next": "Nothing is queued; the next window needs planning.",
        "went_well": "What went well",
        "improve": "What to improve",
        "actions": "Action items",
        "done_count": "Done",
        "in_progress_count": "In progress",
        "planned_count": "Planned",
        "blockers_count": "Blocking relations",
        "hours_logged": "Hours logged",
        "data_notes": "Data notes",
        "unassigned": "Unassigned",
        "no_due": "no due date",
        "more_rows": "… and {count} more; open the full list with list_work_packages.",
        "manual": MANUAL,
    },
    "vi": {
        "title": "BÁO CÁO TUẦN - AGILE SCRUM",
        "generated": "Tự động tạo từ dữ liệu OpenProject",
        "a": "A. THÔNG TIN CHUNG",
        "b": "B. TÓM TẮT ĐIỀU HÀNH",
        "c": "C. DELIVERY & BACKLOG MOVEMENT",
        "d": "D. NGUỒN LỰC & NĂNG LỰC THỰC THI",
        "e": "E. TRỞ NGẠI (IMPEDIMENTS) & PHỤ THUỘC",
        "f": "F. CHẤT LƯỢNG & ỔN ĐỊNH HỆ THỐNG",
        "g": "G. KẾ HOẠCH TUẦN TỚI",
        "h": "H. SPRINT HEALTH & CẢI TIẾN",
        "appendix": "PHỤ LỤC: BẢN SIÊU GỌN CHO LÃNH ĐẠO",
        "field": "Mục",
        "value": "Giá trị",
        "window": "Tuần báo cáo",
        "team": "Team / Squad",
        "product": "Product / Module",
        "project_id": "Project ID",
        "open_now": "Work package đang mở",
        "progress": "Tiến độ so với Sprint Goal",
        "on_track": "Đúng tiến độ",
        "at_risk": "Có rủi ro",
        "off_track": "Chậm tiến độ",
        "top_deliverables": "Deliverables nổi bật (đã Done):",
        "none_done": "Chưa có work package nào hoàn thành trong tuần.",
        "biggest_blocker": "Vướng mắc lớn nhất",
        "no_blockers": "không có",
        "blocked_items": "quan hệ chặn trên công việc đang mở",
        "needs_decision": "Cần hỗ trợ / quyết định",
        "sub_done": "1) Công việc đã hoàn thành (Done)",
        "sub_in_progress": "2) Công việc đang thực hiện (In Progress)",
        "sub_planned": "3) Công việc đề ra nhưng chưa bắt đầu (Planned)",
        "th_ticket": "Ticket",
        "th_summary": "Mô tả ngắn",
        "th_owner": "Owner",
        "th_date": "Hạn / cập nhật cuối",
        "th_status": "Status",
        "none_in_progress": "Không có work package đang in progress.",
        "none_planned": "Không có work package đang chờ bắt đầu.",
        "team_size": "Quy mô team",
        "capacity": "Capacity đã ghi nhận trong tuần",
        "people": "người",
        "hours": "giờ",
        "by_activity": "Phân bổ theo loại việc:",
        "by_person": "Phân bổ theo người:",
        "th_activity": "Loại việc",
        "th_hours": "Giờ",
        "th_share": "Tỷ lệ",
        "th_person": "Người",
        "th_entries": "Số bản ghi",
        "entries_scope": "{scanned}/{total} bản ghi thời gian",
        "no_time": "Không có thời gian nào được log trong tuần (hoặc tài khoản này không thấy).",
        "th_item": "Work package",
        "th_relation": "Quan hệ",
        "th_note": "Ghi chú",
        "no_impediments": "Không tìm thấy quan hệ chặn nào trên các work package đang mở đã quét.",
        "created_by_type": "Work package tạo mới trong tuần, theo loại:",
        "th_type": "Loại",
        "th_count": "Số lượng",
        "open_by_status": "Work package đang mở theo status (đếm phía server):",
        "no_created": "Không có work package nào được tạo trong tuần.",
        "next_priorities": "Top ưu tiên:",
        "none_next": "Chưa có việc nào xếp hàng; tuần tới cần lập kế hoạch.",
        "went_well": "Điều làm tốt",
        "improve": "Điều cần cải thiện",
        "actions": "Action items",
        "done_count": "Done",
        "in_progress_count": "In progress",
        "planned_count": "Planned",
        "blockers_count": "Quan hệ chặn",
        "hours_logged": "Giờ đã log",
        "data_notes": "Ghi chú dữ liệu",
        "unassigned": "Chưa gán",
        "no_due": "chưa có hạn",
        "more_rows": "… và {count} mục nữa; xem đầy đủ bằng list_work_packages.",
        "manual": MANUAL_VI,
    },
}

#: Locales with a full template. Anything else renders English with a note.
LOCALES: tuple[str, ...] = tuple(LABELS)

UNKNOWN_LOCALE_NOTE = (
    "locale {requested!r} has no template on this server; the report below is rendered in "
    "English. Available locales: {available}."
)

WINDOW_NOTE = (
    "no window was given, so the report covers the {days} days ending {end} (server date). "
    "Pass from_date/to_date for a different window."
)

INSTRUCTIONS = (
    "The document below was rendered from live OpenProject data by this server; the numbers "
    "are real and must not be changed, recomputed or embellished. Deliver it as the report, "
    "editing only the lines marked as manual, and read the 'Data notes' block before calling "
    "any figure complete — a capped list or an unreadable source is recorded there. If "
    "something is missing, fetch it with the OpenProject tools rather than inventing it."
)


# --- small rendering helpers ----------------------------------------------


def _labels(locale: str | None) -> tuple[dict[str, str], str | None]:
    """The label table for a locale, plus a note when it fell back (G5)."""
    requested = (locale or DEFAULT_LOCALE).strip().lower()
    if requested in LABELS:
        return LABELS[requested], None
    return (
        LABELS[DEFAULT_LOCALE],
        UNKNOWN_LOCALE_NOTE.format(requested=requested, available=", ".join(LOCALES)),
    )


def _cell(value: str | None, fallback: str = "-") -> str:
    """One markdown table cell: no pipes, no newlines, never empty."""
    text = (value or "").replace("|", "\\|").replace("\n", " ").strip()
    return text or fallback


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def _hours(value: float) -> str:
    return f"{value:.1f}"


def _ref_name(ref: Ref | None, fallback: str) -> str:
    """A ref's display name, falling back to its id and then to ``fallback``."""
    if ref is None:
        return fallback
    if ref.name and ref.name.strip():
        return ref.name
    return str(ref.id) if ref.id is not None else fallback


def _work_package_rows(
    rows: Sequence[ReportWorkPackage], labels: dict[str, str]
) -> list[list[str]]:
    return [
        [
            _cell(f"#{row.id}" if row.id is not None else None),
            _cell(row.subject),
            _cell(_ref_name(row.assignee, labels["unassigned"])),
            _cell(row.due_date or (row.updated_at or "")[:10], labels["no_due"]),
            _cell(_ref_name(row.status, "-")),
        ]
        for row in rows
    ]


def _work_package_table(
    rows: Sequence[ReportWorkPackage], labels: dict[str, str], empty: str
) -> list[str]:
    if not rows:
        return [f"_{empty}_"]
    shown = rows[:TABLE_ROW_LIMIT]
    lines = _table(
        [
            labels["th_ticket"],
            labels["th_summary"],
            labels["th_owner"],
            labels["th_date"],
            labels["th_status"],
        ],
        _work_package_rows(shown, labels),
    )
    if len(rows) > len(shown):
        lines.append("")
        lines.append(labels["more_rows"].format(count=len(rows) - len(shown)))
    return lines


def _dedupe(buckets: Iterable[Sequence[ReportWorkPackage]]) -> list[ReportWorkPackage]:
    """Union of several report buckets, first occurrence wins."""
    seen: set[int | str] = set()
    rows: list[ReportWorkPackage] = []
    for bucket in buckets:
        for row in bucket:
            if row.id is not None:
                if row.id in seen:
                    continue
                seen.add(row.id)
            rows.append(row)
    return rows


def _untouched_since_raised(row: ReportWorkPackage) -> bool:
    """True when nothing has happened to ``row`` since the day it was raised.

    Membership of the window's ``updated`` bucket cannot answer this: a row
    created inside the window always has ``updatedAt >= createdAt``, so every
    freshly raised row is in that bucket too and "did not move" would be empty
    on every real instance. The row's own two timestamps can, compared by date
    because creation itself can bump ``updatedAt`` by a second. A row missing
    either timestamp is never claimed as untouched.
    """
    created = (row.created_at or "")[:10]
    updated = (row.updated_at or "")[:10]
    return bool(created) and created == updated


def _classify(
    data: ProjectReportData,
) -> tuple[list[ReportWorkPackage], list[ReportWorkPackage], list[ReportWorkPackage]]:
    """Split the window into done / in progress / planned using ``is_closed``.

    A row is *done* when the instance flags its status as closed — the only Done
    signal, and never a status name. An open row is *planned* when it was raised
    and left alone (see :func:`_untouched_since_raised`), and *in progress*
    otherwise.
    """
    rows = _dedupe([data.closed.items, data.updated.items, data.created.items])
    done = [row for row in rows if row.is_closed is True]
    open_rows = [row for row in rows if row.is_closed is not True]
    planned = [row for row in open_rows if _untouched_since_raised(row)]
    in_progress = [row for row in open_rows if not _untouched_since_raised(row)]
    return done, in_progress, planned


def _impediment_rows(impediments: Sequence[Impediment], labels: dict[str, str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for item in impediments:
        arrow = "blocked by" if item.direction == "blocked_by" else "blocks"
        related = _ref_name(item.related, "?")
        related_id = item.related.id if item.related else None
        target = f"#{related_id} {related}" if related_id is not None else related
        subject = _ref_name(item.work_package, "")
        own_id = item.work_package.id if item.work_package else None
        rows.append(
            [
                _cell(f"#{own_id} {subject}"),
                _cell(f"{arrow} {target}"),
                _cell(_ref_name(item.assignee, labels["unassigned"])),
                _cell(item.description),
            ]
        )
    return rows


def _failure(title: str, exc: OpenProjectError, guidance: str) -> str:
    """Render an upstream failure as data the model can act on (G4)."""
    return "\n".join(
        [
            f"# {title}",
            "",
            "OpenProject could not be read, so no report was rendered. The structured error:",
            "",
            "```json",
            exc.to_json(),
            "```",
            "",
            guidance,
        ]
    )


def _bad_input(title: str, message: str) -> str:
    return "\n".join([f"# {title}", "", message])


def _iso_date(name: str, value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{name}={value!r} is not an ISO date; use YYYY-MM-DD.") from exc


def _window(from_date: str | None, to_date: str | None) -> tuple[str, str, str | None]:
    """Resolve the report window, saying so when it was derived."""
    if from_date and to_date:
        start, end = _iso_date("from_date", from_date), _iso_date("to_date", to_date)
        if start > end:
            raise ValueError(f"from_date {start} is after to_date {end}.")
        return start.isoformat(), end.isoformat(), None
    end = _iso_date("to_date", to_date) if to_date else dt.date.today()
    start = (
        _iso_date("from_date", from_date)
        if from_date
        else end - dt.timedelta(days=WEEKLY_WINDOW_DAYS - 1)
    )
    if start > end:
        raise ValueError(f"from_date {start} is after to_date {end}.")
    return (
        start.isoformat(),
        end.isoformat(),
        WINDOW_NOTE.format(days=WEEKLY_WINDOW_DAYS, end=end.isoformat()),
    )


# --- the weekly report ----------------------------------------------------


def _health(
    done: Sequence[ReportWorkPackage], in_progress: Sequence[ReportWorkPackage], blockers: int
) -> str:
    """The one-word verdict the executive summary opens with."""
    if blockers:
        return "off_track"
    if len(done) < len(in_progress):
        return "at_risk"
    return "on_track"


def render_weekly_report(
    data: ProjectReportData,
    impediments: Sequence[Impediment],
    *,
    locale: str,
    team_name: str | None,
    extra_notes: Sequence[str],
) -> str:
    """Render the 8-section (A-H) weekly report plus the executive appendix."""
    labels, locale_note = _labels(locale)
    done, in_progress, planned = _classify(data)
    project_id = data.project.id if data.project else None
    notes = [*extra_notes, *([locale_note] if locale_note else []), *data.notes]
    manual = labels["manual"]

    lines: list[str] = [
        f"# {labels['title']}",
        "",
        f"_{labels['generated']}_",
        "",
        f"## {labels['a']}",
        "",
    ]
    lines.extend(
        _table(
            [labels["field"], labels["value"]],
            [
                [labels["window"], f"{data.from_date} - {data.to_date}"],
                [labels["team"], _cell(team_name, manual)],
                [labels["product"], _cell(_ref_name(data.project, "-"))],
                [labels["project_id"], _cell(str(project_id) if project_id is not None else None)],
                [labels["open_now"], str(data.open_total)],
            ],
        )
    )

    health = labels[_health(done, in_progress, len(impediments))]
    lines += ["", f"## {labels['b']}", "", f"**{labels['progress']}:** {health}", ""]
    lines.append(f"**{labels['top_deliverables']}**")
    if done:
        lines.extend(
            f"{index}. #{row.id} - {_cell(row.subject)}"
            for index, row in enumerate(done[:HIGHLIGHT_LIMIT], start=1)
        )
    else:
        lines.append(f"- _{labels['none_done']}_")
    blocker_text = (
        f"{len(impediments)} {labels['blocked_items']}" if impediments else labels["no_blockers"]
    )
    lines += [
        "",
        f"**{labels['biggest_blocker']}:** {blocker_text}",
        "",
        f"**{labels['needs_decision']}:** {manual}",
        "",
        f"## {labels['c']}",
        "",
        f"### {labels['sub_done']} ({len(done)})",
        "",
    ]
    lines.extend(_work_package_table(done, labels, labels["none_done"]))
    lines += ["", f"### {labels['sub_in_progress']} ({len(in_progress)})", ""]
    lines.extend(_work_package_table(in_progress, labels, labels["none_in_progress"]))
    lines += ["", f"### {labels['sub_planned']} ({len(planned)})", ""]
    lines.extend(_work_package_table(planned, labels, labels["none_planned"]))

    lines += ["", f"## {labels['d']}", ""]
    lines.append(f"**{labels['team_size']}:** {len(data.roster)} {labels['people']}")
    lines.append("")
    scope = labels["entries_scope"].format(
        scanned=data.time.entry_count, total=data.time.total_entries
    )
    lines.append(
        f"**{labels['capacity']}:** {_hours(data.time.total_hours)} {labels['hours']} ({scope})"
    )
    lines.append("")
    if data.time.total_hours > 0:
        lines.append(f"**{labels['by_activity']}**")
        lines.append("")
        lines.extend(
            _table(
                [labels["th_activity"], labels["th_hours"], labels["th_share"]],
                [
                    [
                        _cell(_ref_name(item.activity, "-")),
                        _hours(item.hours),
                        f"{item.hours / data.time.total_hours * 100:.1f}%",
                    ]
                    for item in data.time.by_activity
                ],
            )
        )
        lines += ["", f"**{labels['by_person']}**", ""]
        lines.extend(
            _table(
                [labels["th_person"], labels["th_hours"], labels["th_entries"]],
                [
                    [_cell(_ref_name(item.user, "-")), _hours(item.hours), str(item.entries)]
                    for item in data.time.by_user
                ],
            )
        )
    else:
        lines.append(f"_{labels['no_time']}_")

    lines += ["", f"## {labels['e']}", ""]
    if impediments:
        lines.extend(
            _table(
                [labels["th_item"], labels["th_relation"], labels["th_owner"], labels["th_note"]],
                _impediment_rows(impediments, labels),
            )
        )
    else:
        lines.append(f"_{labels['no_impediments']}_")

    lines += ["", f"## {labels['f']}", "", f"**{labels['created_by_type']}**", ""]
    by_type: dict[str, int] = {}
    for row in data.created.items:
        by_type[_ref_name(row.type, "-")] = by_type.get(_ref_name(row.type, "-"), 0) + 1
    if by_type:
        lines.extend(
            _table(
                [labels["th_type"], labels["th_count"]],
                [[_cell(name), str(count)] for name, count in sorted(by_type.items())],
            )
        )
    else:
        lines.append(f"_{labels['no_created']}_")
    lines += ["", f"**{labels['open_by_status']}**", ""]
    lines.extend(
        _table(
            [labels["th_status"], labels["th_count"]],
            [
                [_cell(_ref_name(bucket.status, "-")), str(bucket.count)]
                for bucket in data.open_by_status
            ],
        )
        if data.open_by_status
        else [f"_{manual}_"]
    )

    lines += ["", f"## {labels['g']}", "", f"**{labels['next_priorities']}**"]
    if planned:
        lines.extend(
            f"{index}. #{row.id} {_cell(row.subject)} "
            f"({_ref_name(row.assignee, labels['unassigned'])} - "
            f"{row.due_date or labels['no_due']})"
            for index, row in enumerate(planned[:PRIORITY_LIMIT], start=1)
        )
    else:
        lines.append(f"- _{labels['none_next']}_")

    lines += [
        "",
        f"## {labels['h']}",
        "",
        f"**{labels['went_well']}:** {manual}",
        "",
        f"**{labels['improve']}:** {manual}",
        "",
        f"**{labels['actions']}:** {manual}",
        "",
        "---",
        "",
        f"## {labels['appendix']}",
        "",
        f"- **{labels['progress']}:** {health}",
        f"- **{labels['done_count']}:** {len(done)}",
        f"- **{labels['in_progress_count']}:** {len(in_progress)}",
        f"- **{labels['planned_count']}:** {len(planned)}",
        f"- **{labels['blockers_count']}:** {len(impediments)}",
        f"- **{labels['hours_logged']}:** {_hours(data.time.total_hours)}",
        f"- **{labels['open_now']}:** {data.open_total}",
        "",
        f"### {labels['data_notes']}",
        "",
    ]
    lines.extend(f"- {note}" for note in notes)
    return "\n".join(lines)


# --- registration ---------------------------------------------------------


def register(mcp: FastMCP) -> None:
    """Register the four prompt templates (SPEC §10)."""

    @mcp.prompt(
        name="weekly_report",
        title="OpenProject weekly report",
        tags={_shared.GROUP_REPORTING},
    )
    async def weekly_report(
        project: str,
        from_date: str | None = None,
        to_date: str | None = None,
        locale: str = DEFAULT_LOCALE,
        team_name: str | None = None,
    ) -> str:
        """Render the 8-section Agile/Scrum weekly report for one project, with live data.

        The server does the reading: work packages created, changed and completed in the
        window, the server-side open-by-status counts, logged hours per activity and per
        person, the membership roster, and the blocking relations on open work. Done is
        decided by each status's own `isClosed` flag, so the report is correct on
        translated and renamed workflows; the rest split into Planned (raised in the
        window and untouched since) and In progress.

        Args:
            project: numeric project id or project identifier (the URL slug), from
                list_projects.
            from_date: window start, ISO YYYY-MM-DD. Omit for the 7 days ending to_date.
            to_date: window end, ISO YYYY-MM-DD. Omit for the server's today.
            locale: 'en' (default) or 'vi'; an unknown locale renders English and says so.
            team_name: team or squad name for the header; omitted leaves a manual field.
        """
        try:
            start, end, window_note = _window(from_date, to_date)
        except ValueError as exc:
            return _bad_input("Weekly report", str(exc))

        ctx = _shared.get_tool_context()
        try:
            data = await reporting.collect_report_data(
                ctx, project_id=project, from_date=start, to_date=end
            )
            impediments, impediment_notes = await reporting.collect_impediments(
                ctx, _dedupe([data.updated.items, data.created.items])
            )
        except OpenProjectError as exc:
            return _failure(
                "Weekly report",
                exc,
                "Check the project id with list_projects and the dates, then retry. Nothing "
                "was written to OpenProject.",
            )

        notes = [*([window_note] if window_note else []), *impediment_notes]
        report = render_weekly_report(
            data, impediments, locale=locale, team_name=team_name, extra_notes=notes
        )
        return f"{INSTRUCTIONS}\n\n---\n\n{report}"

    @mcp.prompt(
        name="daily_standup",
        title="OpenProject daily standup",
        tags={_shared.GROUP_REPORTING},
    )
    async def daily_standup(project: str) -> str:
        """Render today's standup for one project: yesterday's movement, what is due today,
        and what is blocked.

        The window is yesterday on the server's clock. Completed items are the ones whose
        status carries the instance's `isClosed` flag, never a status name; "due today"
        is an open-status query on today's date; blockers are the `blocks`/`blocked`
        relations visible on the open work packages that moved.

        Args:
            project: numeric project id or project identifier (the URL slug), from
                list_projects.
        """
        ctx = _shared.get_tool_context()
        today = dt.date.today()
        yesterday = (today - dt.timedelta(days=1)).isoformat()
        try:
            data = await reporting.collect_report_data(
                ctx, project_id=project, from_date=yesterday, to_date=yesterday
            )
            due_today = await reporting.collect_due_on(
                ctx, project_id=project, date=today.isoformat()
            )
            impediments, impediment_notes = await reporting.collect_impediments(
                ctx, data.updated.items
            )
        except OpenProjectError as exc:
            return _failure(
                "Daily standup",
                exc,
                "Check the project id with list_projects, then retry.",
            )

        labels = LABELS[DEFAULT_LOCALE]
        done, in_progress, _ = _classify(data)
        lines = [
            f"# Daily standup - {_ref_name(data.project, project)}",
            "",
            f"_Yesterday: {yesterday} · today: {today.isoformat()}_",
            "",
            f"## Completed yesterday ({len(done)})",
            "",
            *_work_package_table(done, labels, "Nothing was completed yesterday."),
            "",
            f"## Moved yesterday, still open ({len(in_progress)})",
            "",
            *_work_package_table(in_progress, labels, "Nothing open changed yesterday."),
            "",
            f"## Due today ({due_today.total})",
            "",
            *_work_package_table(due_today.items, labels, "Nothing is due today."),
            "",
            f"## Blocked ({len(impediments)})",
            "",
        ]
        if impediments:
            lines.extend(
                _table(
                    ["Work package", "Relation", "Owner", "Note"],
                    _impediment_rows(impediments, labels),
                )
            )
        else:
            lines.append("_No blocking relations were found on the work packages scanned._")
        lines += [
            "",
            f"## Time logged yesterday: {_hours(data.time.total_hours)} h",
            "",
            f"Open work packages right now: {data.open_total}.",
            "",
            "### Data notes",
            "",
            *(f"- {note}" for note in [*data.notes, *impediment_notes]),
        ]
        standup = "\n".join(lines)
        return f"{INSTRUCTIONS}\n\n---\n\n{standup}"

    @mcp.prompt(
        name="triage_inbox",
        title="OpenProject inbox triage",
        tags={_shared.GROUP_NOTIFICATIONS},
    )
    async def triage_inbox() -> str:
        """Group the current user's unread OpenProject notifications by reason and suggest
        what to do with each group.

        Reads the unread inbox server-side and renders one section per reason (mentioned,
        assigned, watched, …) with the work packages behind it, so triage is a single pass
        instead of one notification at a time.
        """
        ctx = _shared.get_tool_context()
        try:
            inbox = await reporting.collect_unread_notifications(ctx)
        except OpenProjectError as exc:
            return _failure(
                "Inbox triage",
                exc,
                "Notifications need an authenticated user; check the credential with "
                "get_instance_info, then retry.",
            )
        return f"{INSTRUCTIONS}\n\n---\n\n{render_inbox(inbox)}"

    @mcp.prompt(
        name="groom_backlog",
        title="OpenProject backlog grooming",
        tags={_shared.GROUP_WORK_PACKAGES},
    )
    async def groom_backlog(project: str) -> str:
        """Sweep a project's open backlog for the three things that rot it: work with no
        assignee, work with no estimate, and work nobody has touched in weeks.

        The open set is read oldest-changed first and classified here, because "has no
        estimate" is not expressible as an OpenProject filter. Counts always come from the
        server, so a capped scan understates the lists but never the backlog.

        Args:
            project: numeric project id or project identifier (the URL slug), from
                list_projects.
        """
        ctx = _shared.get_tool_context()
        try:
            sweep = await reporting.collect_backlog_sweep(ctx, project_id=project)
        except OpenProjectError as exc:
            return _failure(
                "Backlog grooming",
                exc,
                "Check the project id with list_projects, then retry.",
            )
        return f"{INSTRUCTIONS}\n\n---\n\n{render_backlog(sweep, project)}"


def render_inbox(inbox: InboxSummary) -> str:
    """Render the unread inbox grouped by reason, with a suggested action per group."""
    actions = {
        "mentioned": "Someone asked you something directly - read the comment and reply, or "
                     "hand it over explicitly.",
        "assigned": "You now own these - accept them, re-assign them, or say when they will "
                    "move.",
        "responsible": "You are accountable for these - check they have an owner and a date.",
        "watched": "You are watching these - skim for changes that affect your work, then mark "
                   "them read.",
        "subscribed": "Subscription noise - mark read unless something changed materially.",
        "commented": "New discussion - read the thread before it forks.",
        "created": "New work landed in your scope - triage type, priority and assignee.",
        "processed": "Status moved - confirm the next step has an owner.",
        "prioritized": "Priority changed - re-check your plan for the day.",
        "scheduled": "Dates moved - check nothing downstream now conflicts.",
        "shared": "Something was shared with you - open it and decide whether to keep access.",
        "reminder": "A reminder you set - act on it or reschedule it.",
        "dateAlert": "A date is approaching or has passed - confirm the date is still real.",
        "unknown": "The API reported no reason - open the item to see what changed.",
    }
    lines = [
        "# Inbox triage",
        "",
        f"_{inbox.total_unread} unread notification(s); {inbox.scanned} scanned._",
        "",
    ]
    if not inbox.groups:
        lines.append("_The inbox is empty - nothing to triage._")
        return "\n".join(lines)

    for group in inbox.groups:
        lines += [f"## {group.reason} ({group.count})", ""]
        lines.extend(
            _table(
                ["Notification", "Work package", "Project"],
                [
                    [
                        _cell(f"#{item.id}"),
                        _cell(
                            f"#{item.resource_id} {item.subject or ''}"
                            if item.resource_id is not None
                            else item.subject
                        ),
                        _cell(_ref_name(item.project, "-")),
                    ]
                    for item in group.items
                ],
            )
        )
        lines += [
            "",
            f"**Suggested action:** {actions.get(group.reason, actions['unknown'])}",
            "",
            "Mark them read with "
            f"`mark_notifications(ids={[item.id for item in group.items]}, read=true)`.",
            "",
        ]
    if inbox.truncated:
        lines.append(
            f"_Only the first {inbox.scanned} of {inbox.total_unread} unread notifications were "
            "scanned; re-run after triaging this batch._"
        )
    return "\n".join(lines)


def render_backlog(sweep: BacklogSweep, project: str) -> str:
    """Render the backlog sweep as three actionable tables."""

    def rows(items: Sequence[BacklogItem]) -> list[list[str]]:
        return [
            [
                _cell(f"#{item.id}"),
                _cell(item.subject),
                _cell(_ref_name(item.type, "-")),
                _cell(_ref_name(item.assignee, "Unassigned")),
                _cell((item.updated_at or "")[:10]),
            ]
            for item in items[:TABLE_ROW_LIMIT]
        ]

    headers = ["Ticket", "Summary", "Type", "Owner", "Last change"]
    lines = [
        f"# Backlog grooming - project {project}",
        "",
        f"_{sweep.open_total} open work package(s); {sweep.scanned} scanned, oldest change "
        f"first. 'Stale' means untouched since {sweep.stale_before}._",
        "",
        f"## No assignee ({len(sweep.unassigned)})",
        "",
        *(
            _table(headers, rows(sweep.unassigned))
            if sweep.unassigned
            else ["_Every scanned item has an owner._"]
        ),
        "",
        f"## No estimate ({len(sweep.unestimated)})",
        "",
        *(
            _table(headers, rows(sweep.unestimated))
            if sweep.unestimated
            else ["_Every scanned item is estimated._"]
        ),
        "",
        f"## Stale ({len(sweep.stale)})",
        "",
        *(
            _table(headers, rows(sweep.stale))
            if sweep.stale
            else ["_Everything scanned moved recently._"]
        ),
        "",
        "For each row decide one of: assign it, estimate it, re-prioritize it, or close it "
        "with `update_work_package(id, status=<a closed status>)`. Ask before closing "
        "anything.",
        "",
    ]
    if sweep.truncated:
        lines.append(
            f"_Only the first {sweep.scanned} of {sweep.open_total} open work packages were "
            "scanned; re-run after this batch._"
        )
    return "\n".join(lines)

