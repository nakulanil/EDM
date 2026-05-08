"""
report_views.py  —  Exam Duty Reports
--------------------------------------
  1. /teachers/reports/                → HTML landing page (staff only)
  2. /teachers/reports/by-date/        → PDF: one page per exam date
  3. /teachers/reports/by-teacher/     → PDF: one page per teacher

Both PDFs accept ?session_id=<pk> — defaults to the most recent session
if not supplied.  All sessions (active, upcoming, completed) are available.
"""

import io
from datetime import date

from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse
from django.shortcuts import render, get_object_or_404

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, HRFlowable,
)

from .models import Booking, ExamDate, Session, Slot, Teacher


# ── Colour palette ────────────────────────────────────────────────────────────
PRIMARY   = colors.HexColor("#1a73e8")
DARK      = colors.HexColor("#1a1a2e")
LIGHT_BG  = colors.HexColor("#f0f4f8")
ROW_ALT   = colors.HexColor("#e8f0fe")
HEADER_BG = colors.HexColor("#1a73e8")
WHITE     = colors.white
GREY      = colors.HexColor("#888888")
MORNING_C = colors.HexColor("#fff8e1")
EVENING_C = colors.HexColor("#e8eaf6")
GREEN     = colors.HexColor("#2e7d32")


# ── Shared helpers ────────────────────────────────────────────────────────────

def _cell_style():
    """ParagraphStyle for wrapped table body cells."""
    styles = getSampleStyleSheet()
    return ParagraphStyle(
        "cell",
        parent=styles["Normal"],
        fontSize=8.5,
        leading=11,
    )


def _header_cell_style():
    styles = getSampleStyleSheet()
    return ParagraphStyle(
        "hcell",
        parent=styles["Normal"],
        fontSize=9,
        leading=11,
        fontName="Helvetica-Bold",
        textColor=WHITE,
    )


def _base_table_style():
    return [
        ("BACKGROUND",    (0, 0), (-1, 0), HEADER_BG),
        ("TEXTCOLOR",     (0, 0), (-1, 0), WHITE),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
        ("TOPPADDING",    (0, 0), (-1, 0), 8),
        ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",      (0, 1), (-1, -1), 8.5),
        ("TOPPADDING",    (0, 1), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 7),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 7),
        ("GRID",          (0, 0), (-1, -1), 0.4, colors.HexColor("#d0d0d0")),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [WHITE, ROW_ALT]),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("ALIGN",         (0, 0), (-1, -1), "LEFT"),
    ]


def _page_header(story, styles, title, subtitle=""):
    story.append(Spacer(1, 0.2 * cm))
    story.append(Paragraph(
        "Deen Dayal Upadhyay College",
        ParagraphStyle("inst", parent=styles["Normal"],
                       fontSize=9, textColor=GREY, spaceAfter=2)
    ))
    story.append(Paragraph(
        title,
        ParagraphStyle("rptitle", parent=styles["Title"],
                       fontSize=16, textColor=DARK, spaceAfter=4)
    ))
    if subtitle:
        story.append(Paragraph(
            subtitle,
            ParagraphStyle("sub", parent=styles["Normal"],
                           fontSize=9, textColor=GREY, spaceAfter=6)
        ))
    story.append(HRFlowable(width="100%", thickness=1.5,
                             color=PRIMARY, spaceAfter=12))


def _resolve_session(request):
    session_id = request.GET.get("session_id")
    if session_id:
        return get_object_or_404(Session, pk=session_id)
    return (
        Session.objects.filter(status="active").first()
        or Session.objects.order_by("-created_at").first()
    )


def _wrap(text, style):
    """Wrap text in a Paragraph, escaping XML-special characters."""
    safe = (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))
    return Paragraph(safe, style)


# ─────────────────────────────────────────────────────────────────────────────
#  Report 1 — By Date
# ─────────────────────────────────────────────────────────────────────────────

@staff_member_required
def report_by_date(request):
    session = _resolve_session(request)
    if not session:
        return HttpResponse("No sessions found in the database.", status=404)

    exam_dates = (
        ExamDate.objects
        .filter(session=session)
        .prefetch_related("slots__booking_set__teacher__department")
        .order_by("date")
    )

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=1.8*cm, rightMargin=1.8*cm,
        topMargin=1.8*cm,  bottomMargin=1.8*cm,
    )

    styles    = getSampleStyleSheet()
    cell_st   = _cell_style()
    hcell_st  = _header_cell_style()
    story     = []
    first_page = True

    # #  | FacultyID | Name  | Dept  | Phone | Assignment
    col_widths = [0.9*cm, 2.3*cm, 4.6*cm, 3.4*cm, 2.8*cm, 2.5*cm]

    for exam_date in exam_dates:
        if not first_page:
            story.append(PageBreak())
        first_page = False

        _page_header(
            story, styles,
            title=f"Exam Duty Sheet — {exam_date.date.strftime('%d %B %Y')}",
            subtitle=(
                f"Session: {session.name}   |   "
                f"Status: {session.get_status_display()}   |   "
                f"Generated: {date.today().strftime('%d %b %Y')}"
            )
        )

        for slot in exam_date.slots.order_by("shift"):
            shift_color = MORNING_C if slot.shift == "Morning" else EVENING_C
            if slot.shift == "Morning":
                time_str = (f"{exam_date.morning_start.strftime('%H:%M')}"
                            f"–{exam_date.morning_end.strftime('%H:%M')}")
            else:
                time_str = (f"{exam_date.evening_start.strftime('%H:%M')}"
                            f"–{exam_date.evening_end.strftime('%H:%M')}")

            story.append(Paragraph(
                f"{slot.shift} Shift  ({time_str})",
                ParagraphStyle("shift", parent=styles["Heading3"],
                               fontSize=10, textColor=DARK,
                               backColor=shift_color,
                               borderPad=4, spaceAfter=4, spaceBefore=8)
            ))

            bookings = (
                Booking.objects
                .filter(slot=slot)
                .select_related("teacher__department")
                .order_by("teacher__full_name")
            )

            if not bookings.exists():
                story.append(Paragraph(
                    "  No teachers assigned to this shift.",
                    ParagraphStyle("empty", parent=styles["Normal"],
                                   fontSize=8.5, textColor=GREY,
                                   spaceAfter=8, leftIndent=10)
                ))
                continue

            header_row = [_wrap(h, hcell_st) for h in
                          ["#", "Faculty ID", "Teacher Name",
                           "Department", "Phone", "Assignment"]]
            rows = [header_row]

            for i, booking in enumerate(bookings, 1):
                t = booking.teacher
                is_auto = booking.auto_assigned
                asgn_st = ParagraphStyle(
                    "asgn", parent=cell_st,
                    textColor=GREY if is_auto else GREEN,
                    fontName="Helvetica-Bold",
                )
                rows.append([
                    _wrap(str(i),                                         cell_st),
                    _wrap(t.faculty_id or "—",                            cell_st),
                    _wrap(t.full_name,                                    cell_st),
                    _wrap(t.department.name if t.department else "—",     cell_st),
                    _wrap(t.phone_number or "—",                          cell_st),
                    _wrap("Auto" if is_auto else "Self-booked",           asgn_st),
                ])

            tbl = Table(rows, colWidths=col_widths, repeatRows=1)
            tbl.setStyle(TableStyle(_base_table_style()))
            story.append(tbl)

        total_assigned  = Booking.objects.filter(slot__exam_date=exam_date).count()
        total_positions = sum(s.total_positions for s in exam_date.slots.all())
        story.append(Spacer(1, 0.4*cm))
        story.append(Paragraph(
            f"Total assigned: <b>{total_assigned}</b> / {total_positions} positions",
            ParagraphStyle("summary", parent=styles["Normal"],
                           fontSize=9, textColor=DARK,
                           borderPad=6, backColor=LIGHT_BG,
                           borderColor=PRIMARY, borderWidth=0.5)
        ))

    if not exam_dates.exists():
        story.append(Paragraph(
            f"No exam dates found for session: {session.name}",
            styles["Normal"]
        ))

    doc.build(story)
    buffer.seek(0)

    safe_name = session.name.replace(" ", "_")
    response = HttpResponse(buffer, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="duty_by_date_{safe_name}.pdf"'
    )
    return response


# ─────────────────────────────────────────────────────────────────────────────
#  Report 2 — By Teacher
# ─────────────────────────────────────────────────────────────────────────────

@staff_member_required
def report_by_teacher(request):
    session = _resolve_session(request)
    if not session:
        return HttpResponse("No sessions found in the database.", status=404)

    teachers = (
        Teacher.objects
        .filter(booking__slot__exam_date__session=session)
        .distinct()
        .order_by("department__name", "full_name")
    )

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=1.8*cm, rightMargin=1.8*cm,
        topMargin=1.8*cm,  bottomMargin=1.8*cm,
    )

    styles    = getSampleStyleSheet()
    cell_st   = _cell_style()
    hcell_st  = _header_cell_style()
    story     = []
    first_page = True

    # #  | Date | Exam(s) [wide] | Shift | ShiftTime | Assignment
    col_widths = [0.8*cm, 2.6*cm, 5.8*cm, 2.0*cm, 2.8*cm, 2.5*cm]

    for teacher in teachers:
        if not first_page:
            story.append(PageBreak())
        first_page = False

        dept = teacher.department.name if teacher.department else "—"
        _page_header(
            story, styles,
            title=teacher.full_name,
            subtitle=(
                f"Faculty ID: {teacher.faculty_id}   |   "
                f"Department: {dept}   |   "
                f"Phone: {teacher.phone_number or '—'}   |   "
                f"Session: {session.name} ({session.get_status_display()})"
            )
        )

        bookings = (
            Booking.objects
            .filter(teacher=teacher, slot__exam_date__session=session)
            .select_related("slot__exam_date")
            .order_by("slot__exam_date__date", "slot__shift")
        )

        header_row = [_wrap(h, hcell_st) for h in
                      ["#", "Date", "Exam(s)", "Shift", "Shift Time", "Assignment"]]
        rows = [header_row]
        total_morning = total_evening = auto_count = 0

        for i, booking in enumerate(bookings, 1):
            slot      = booking.slot
            exam_date = slot.exam_date

            exams      = exam_date.exams.filter(session=slot.shift)
            exam_names = ", ".join(e.exam_name for e in exams) or "—"

            if slot.shift == "Morning":
                shift_time = (f"{exam_date.morning_start.strftime('%H:%M')}"
                              f"–{exam_date.morning_end.strftime('%H:%M')}")
                total_morning += 1
            else:
                shift_time = (f"{exam_date.evening_start.strftime('%H:%M')}"
                              f"–{exam_date.evening_end.strftime('%H:%M')}")
                total_evening += 1

            is_auto = booking.auto_assigned
            if is_auto:
                auto_count += 1

            shift_bg_st = ParagraphStyle(
                "sbg", parent=cell_st,
                backColor=MORNING_C if slot.shift == "Morning" else EVENING_C
            )
            asgn_st = ParagraphStyle(
                "asgn", parent=cell_st,
                textColor=GREY if is_auto else GREEN,
                fontName="Helvetica-Bold",
            )

            rows.append([
                _wrap(str(i),                              cell_st),
                _wrap(exam_date.date.strftime("%d %b %Y"), cell_st),
                _wrap(exam_names,                          cell_st),   # wraps freely
                _wrap(slot.shift,                          shift_bg_st),
                _wrap(shift_time,                          cell_st),
                _wrap("Auto" if is_auto else "Self-booked", asgn_st),
            ])

        tbl = Table(rows, colWidths=col_widths, repeatRows=1)
        tbl.setStyle(TableStyle(_base_table_style()))
        story.append(tbl)

        # Summary box
        total       = len(bookings)
        self_booked = total - auto_count
        story.append(Spacer(1, 0.5*cm))

        sum_hst = ParagraphStyle("sh", parent=styles["Normal"],
                                 fontSize=8.5, fontName="Helvetica-Bold",
                                 textColor=WHITE, alignment=1)
        sum_vst = ParagraphStyle("sv", parent=styles["Normal"],
                                 fontSize=13, fontName="Helvetica-Bold",
                                 textColor=PRIMARY, alignment=1)
        summary_data = [
            [_wrap(h, sum_hst) for h in
             ["Total Duties", "Morning", "Evening", "Self-booked", "Auto-assigned"]],
            [_wrap(str(v), sum_vst) for v in
             [total, total_morning, total_evening, self_booked, auto_count]],
        ]
        summary_tbl = Table(summary_data, colWidths=[3.3*cm]*5)
        summary_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), DARK),
            ("ROWBACKGROUNDS",(0, 1), (-1, 1), [LIGHT_BG]),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING",    (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("GRID",          (0, 0), (-1, -1), 0.4, colors.HexColor("#d0d0d0")),
        ]))
        story.append(summary_tbl)

    if not teachers.exists():
        story.append(Paragraph(
            f"No bookings found for session: {session.name}",
            styles["Normal"]
        ))

    doc.build(story)
    buffer.seek(0)

    safe_name = session.name.replace(" ", "_")
    response = HttpResponse(buffer, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="duty_by_teacher_{safe_name}.pdf"'
    )
    return response


# ─────────────────────────────────────────────────────────────────────────────
#  Reports Landing Page
# ─────────────────────────────────────────────────────────────────────────────

@staff_member_required
def reports_landing(request):
    all_sessions = Session.objects.order_by("-created_at")

    session_id = request.GET.get("session_id")
    if session_id:
        selected = get_object_or_404(Session, pk=session_id)
    else:
        selected = (
            Session.objects.filter(status="active").first()
            or all_sessions.first()
        )

    stats = {}
    if selected:
        stats["total_dates"]     = ExamDate.objects.filter(session=selected).count()
        stats["total_bookings"]  = Booking.objects.filter(
            slot__exam_date__session=selected).count()
        stats["total_teachers"]  = (
            Teacher.objects
            .filter(booking__slot__exam_date__session=selected)
            .distinct().count()
        )
        stats["total_positions"] = sum(
            s.total_positions
            for s in Slot.objects.filter(exam_date__session=selected)
        )
        stats["unfilled"] = stats["total_positions"] - stats["total_bookings"]

    return render(request, "reports/landing.html", {
        "selected":     selected,
        "all_sessions": all_sessions,
        "stats":        stats,
    })