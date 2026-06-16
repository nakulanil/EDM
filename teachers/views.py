import random
from django.db import models as db_models
from django.db.models import Q
from datetime import timedelta
from django.utils import timezone
from django.shortcuts import get_object_or_404, render, redirect
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.db import transaction
from django.core.mail import send_mail
from django.contrib.auth.password_validation import validate_password

from teachers.models import (
    Booking, Session, Slot, Teacher,
    ExamDate, Notification, TeacherUnavailability, SwapRequest
)


# REMOVED: teacher_details view — it was never wired up in urls.py and
# referenced a template (teacher_details.html) that doesn't exist.
# It was completely unreachable dead code.


# ─── Exam Slots Count ─────────────────────────────────────────────────────────
# Used by slot_hint.js in the admin session setup page.

def exam_slots_count(request, exam_date_id):
    exam = get_object_or_404(ExamDate, id=exam_date_id)
    slots = Slot.objects.filter(exam_date=exam).count()
    return JsonResponse({'slots_count': slots})


# ─── Dashboard ────────────────────────────────────────────────────────────────

@login_required
def dashboard(request):
    teacher = request.user.teacher
    current_time = timezone.now()

    active_session = Session.objects.filter(status__in=['active', 'completed']).order_by('-created_at').first()

    slots_with_seats = []
    my_bookings = Booking.objects.none()
    notifications = Notification.objects.filter(teacher=teacher, is_read=False)

    if active_session:
        all_slots = Slot.objects.select_related('exam_date').filter(
            exam_date__session=active_session
        ).order_by('exam_date__date', 'shift')

        my_bookings = Booking.objects.filter(
            teacher=teacher,
            slot__exam_date__session=active_session
        ).select_related('slot__exam_date')

        booked_slot_ids = list(my_bookings.values_list('slot_id', flat=True))

        for slot in all_slots:
            slot.seats_left = slot.total_positions - slot.filled_positions
            slot.already_booked = slot.id in booked_slot_ids
            slot.booking_closed = current_time > slot.exam_date.booking_deadline

            if not slot.booking_closed:
                time_left = slot.exam_date.booking_deadline - current_time
                slot.time_remaining_seconds = int(time_left.total_seconds())
            else:
                slot.time_remaining_seconds = 0

            slots_with_seats.append(slot)

        for booking in my_bookings:
            booking.slot.booking_closed = (
                current_time > booking.slot.exam_date.booking_deadline
            )

    # Count pending swap requests for notification badge
    pending_swaps_count = SwapRequest.objects.filter(
        target=teacher, status='pending'
    ).count()

    # ── Unavailability ──────────────────────────────────────────────
    my_unavailability = TeacherUnavailability.objects.filter(
        teacher=teacher
    ).order_by('date')

    return render(request, 'dashboard.html', {
        'teacher': teacher,
        'active_session': active_session,
        'slots': slots_with_seats,
        'my_bookings': my_bookings,
        'notifications': notifications,
        'my_unavailability': my_unavailability,  
        'pending_swaps_count': pending_swaps_count,
    })


# ─── Notifications ────────────────────────────────────────────────────────────

@login_required
def mark_notifications_read(request):
    Notification.objects.filter(
        teacher=request.user.teacher,
        is_read=False
    ).update(is_read=True)
    return JsonResponse({'status': 'ok'})


# ─── Dashboard API ────────────────────────────────────────────────────────────

@login_required
def dashboard_data(request):
    teacher = request.user.teacher
    current_time = timezone.now()

    active_session = Session.objects.filter(status__in=['active', 'completed']).order_by('-created_at').first()

    unread_notifications = list(
        Notification.objects.filter(
            teacher=teacher,
            is_read=False
        ).values('id', 'activity', 'message', 'created_at')
    )

    for n in unread_notifications:
        n['created_at'] = str(n['created_at'])

    slots_data = []
    bookings_data = []

    if active_session:
        all_slots = Slot.objects.select_related('exam_date').filter(
            exam_date__session=active_session
        ).order_by('exam_date__date', 'shift')

        my_bookings = Booking.objects.filter(
            teacher=teacher,
            slot__exam_date__session=active_session
        ).select_related('slot__exam_date')

        booked_slot_ids = list(my_bookings.values_list('slot_id', flat=True))

        for slot in all_slots:
            seats_left = slot.total_positions - slot.filled_positions
            booking_closed = current_time > slot.exam_date.booking_deadline

            time_remaining_seconds = None
            if not booking_closed:
                time_left = slot.exam_date.booking_deadline - current_time
                time_remaining_seconds = int(time_left.total_seconds())

            slots_data.append({
                'id': slot.id,
                'date': str(slot.exam_date.date),
                'shift': slot.shift,
                'seats_left': seats_left,
                'already_booked': slot.id in booked_slot_ids,
                'booking_closed': booking_closed,
                'time_remaining_seconds': time_remaining_seconds,
            })

        for booking in my_bookings:
            booking_closed = current_time > booking.slot.exam_date.booking_deadline
            bookings_data.append({
                'id': booking.id,
                'date': str(booking.slot.exam_date.date),
                'shift': booking.slot.shift,
                'booking_closed': booking_closed,
                'auto_assigned': booking.auto_assigned,
            })

    return JsonResponse({
        'duties': teacher.duties,
        'slots': slots_data,
        'bookings': bookings_data,
        'notifications': unread_notifications,
    })


# ─── Book Slot ────────────────────────────────────────────────────────────────

@login_required
def book_slot(request, slot_id):
    teacher = request.user.teacher

    with transaction.atomic():
        slot = Slot.objects.select_for_update().get(id=slot_id)

        if hasattr(teacher, 'deputy'):
            messages.error(request, "Deputy Superintendents are not assigned slot duties.")
            return redirect('dashboard')

        if timezone.now() > slot.exam_date.booking_deadline:
            messages.error(request, "Booking deadline has passed.")
            return redirect('dashboard')

        if slot.filled_positions >= slot.total_positions:
            messages.error(request, "Slot is full.")
            return redirect('dashboard')

        if Booking.objects.filter(teacher=teacher, slot=slot).exists():
            messages.error(request, "You already booked this slot.")
            return redirect('dashboard')

        if TeacherUnavailability.objects.filter(
            teacher=teacher,
            date=slot.exam_date.date
        ).exists():
            messages.error(request, "You are unavailable on this date.")
            return redirect('dashboard')

        same_day = Booking.objects.filter(
            teacher=teacher,
            slot__exam_date=slot.exam_date
        ).count()

        if same_day >= 2:
            messages.error(request, "Max 2 duties per day allowed.")
            return redirect('dashboard')

        if Booking.objects.filter(
            teacher=teacher,
            slot__exam_date=slot.exam_date,
            slot__shift=slot.shift
        ).exists():
            messages.error(request, "You already have this shift.")
            return redirect('dashboard')

        if teacher.duties <= 0:
            messages.error(request, "No duties remaining.")
            return redirect('dashboard')

        Booking.objects.create(teacher=teacher, slot=slot)

        slot.filled_positions += 1
        slot.save()

        teacher.duties -= 1
        teacher.save()

    Notification.objects.create(
        teacher=teacher,
        activity='booked',
        message=f"You booked {slot.shift} on {slot.exam_date.date}."
    )

    messages.success(request, "Slot booked successfully.")
    return redirect('dashboard')


# ─── Cancel Booking ───────────────────────────────────────────────────────────

@login_required
def cancel_booking(request, booking_id):
    teacher = request.user.teacher

    with transaction.atomic():
        booking = Booking.objects.select_related('slot').get(
            id=booking_id,
            teacher=teacher
        )
        slot = Slot.objects.select_for_update().get(id=booking.slot.id)

        if timezone.now() > slot.exam_date.booking_deadline:
            messages.error(request, "Cancellation deadline passed.")
            return redirect('dashboard')

        if slot.filled_positions > 0:
            slot.filled_positions -= 1
            slot.save()

        teacher.duties += 1
        teacher.save()

        booking.delete()

    Notification.objects.create(
        teacher=teacher,
        activity='cancelled',
        message=f"You cancelled {slot.shift} on {slot.exam_date.date}."
    )

    messages.success(request, "Booking cancelled.")
    return redirect('dashboard')


# ─── Change Password ──────────────────────────────────────────────────────────

@login_required
def change_password(request):
    if request.method == 'POST':
        old = request.POST.get('old_password', '').strip()
        new = request.POST.get('new_password', '').strip()
        confirm = request.POST.get('confirm_password', '').strip()

        user = authenticate(username=request.user.username, password=old)
        if not user:
            messages.error(request, "Incorrect old password.")
            return render(request, 'change_password.html')

        if new != confirm:
            messages.error(request, "Passwords do not match.")
            return render(request, 'change_password.html')

        try:
            validate_password(new)
        except Exception as e:
            messages.error(request, str(e))
            return render(request, 'change_password.html')

        request.user.set_password(new)
        request.user.save()

        messages.success(request, "Password changed. Please log in again.")
        return redirect('login')

    return render(request, 'change_password.html')


# ─── Forgot Password ──────────────────────────────────────────────────────────

def forgot_password(request):
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()

        # FIX: was bare `except:` which swallowed ALL exceptions silently.
        # Now catches only the specific cases we expect.
        try:
            user = User.objects.get(username=username)
            teacher = user.teacher
        except User.DoesNotExist:
            messages.error(request, "No account found with that username.")
            return render(request, 'forgot_password.html')
        except Teacher.DoesNotExist:
            messages.error(request, "No teacher profile linked to this account.")
            return render(request, 'forgot_password.html')

        if not teacher.email:
            messages.error(request, "No email address on file. Contact admin.")
            return render(request, 'forgot_password.html')

        otp = str(random.randint(100000, 999999))
        teacher.otp = otp
        teacher.otp_expiry = timezone.now() + timedelta(minutes=5)
        teacher.save()

        try:
            send_mail(
                'Your OTP — Exam Duty System',
                f'Your OTP for password reset is: {otp}\n\nIt expires in 5 minutes.',
                None,
                [teacher.email],
                fail_silently=False
            )
        except Exception:
            messages.error(request, "Failed to send OTP email. Please try again.")
            return render(request, 'forgot_password.html')

        request.session['reset_username'] = username
        return redirect('forgot_password_verify')

    return render(request, 'forgot_password.html')


def forgot_password_verify(request):
    username = request.session.get('reset_username')
    if not username:
        return redirect('forgot_password')

    # FIX: `User` and `Teacher` are already imported at the top —
    # removed the redundant inline `from django.contrib.auth.models import User` imports.
    try:
        user = User.objects.get(username=username)
        teacher = user.teacher
    except (User.DoesNotExist, Teacher.DoesNotExist):
        messages.error(request, "Session expired. Please start again.")
        return redirect('forgot_password')

    if request.method == 'POST':
        otp = request.POST.get('otp', '').strip()
        new = request.POST.get('new_password', '').strip()
        confirm = request.POST.get('confirm_password', '').strip()

        if not teacher.otp_expiry or timezone.now() > teacher.otp_expiry:
            messages.error(request, "OTP has expired. Please request a new one.")
            return redirect('forgot_password')

        if otp != teacher.otp:
            messages.error(request, "Invalid OTP.")
            return render(request, 'forgot_password_verify.html')

        if new != confirm:
            messages.error(request, "Passwords do not match.")
            return render(request, 'forgot_password_verify.html')

        try:
            validate_password(new)
        except Exception as e:
            messages.error(request, str(e))
            return render(request, 'forgot_password_verify.html')

        user.set_password(new)
        user.save()

        # Clear OTP fields after successful reset
        teacher.otp = None
        teacher.otp_expiry = None
        teacher.save()

        del request.session['reset_username']

        messages.success(request, "Password reset successful. Please log in.")
        return redirect('login')

    return render(request, 'forgot_password_verify.html')

# ─── Download Report ─────────────────────────────────────────────────────────

@login_required
def download_my_report(request):
    import io
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    from django.http import HttpResponse

    teacher = request.user.teacher

    session = (
        Session.objects.filter(
            status='completed',
            exam_dates__slots__booking__teacher=teacher
        ).distinct().order_by('-created_at').first()
    )

    if not session:
        messages.error(request, "No completed session report available.")
        return redirect('dashboard')

    bookings = (
        Booking.objects.filter(
            teacher=teacher,
            slot__exam_date__session=session
        ).select_related('slot__exam_date').order_by('slot__exam_date__date', 'slot__shift')
    )

    if not bookings.exists():
        messages.error(request, "No bookings found for your account.")
        return redirect('dashboard')

    # ── PDF Setup ──
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=1.8*cm, rightMargin=1.8*cm,
        topMargin=1.8*cm, bottomMargin=1.8*cm,
    )

    styles = getSampleStyleSheet()
    PRIMARY   = colors.HexColor("#1a73e8")
    DARK      = colors.HexColor("#1a1a2e")
    LIGHT_BG  = colors.HexColor("#f0f4f8")
    ROW_ALT   = colors.HexColor("#e8f0fe")
    WHITE     = colors.white
    GREY      = colors.HexColor("#888888")
    GREEN     = colors.HexColor("#2e7d32")
    MORNING_C = colors.HexColor("#fff8e1")
    EVENING_C = colors.HexColor("#e8eaf6")

    story = []

    # ── Header ──
    story.append(Spacer(1, 0.2*cm))
    story.append(Paragraph(
        "Deen Dayal Upadhyay College",
        ParagraphStyle("inst", parent=styles["Normal"], fontSize=9, textColor=GREY, spaceAfter=2)
    ))
    story.append(Paragraph(
        f"Exam Duty Report — {teacher.full_name}",
        ParagraphStyle("title", parent=styles["Title"], fontSize=16, textColor=DARK, spaceAfter=4)
    ))
    story.append(Paragraph(
        f"Faculty ID: {teacher.faculty_id}   |   "
        f"Department: {teacher.department.name if teacher.department else '—'}   |   "
        f"Session: {session.name}",
        ParagraphStyle("sub", parent=styles["Normal"], fontSize=9, textColor=GREY, spaceAfter=6)
    ))
    story.append(HRFlowable(width="100%", thickness=1.5, color=PRIMARY, spaceAfter=12))

    # ── Table ──
    cell_st  = ParagraphStyle("cell",  parent=styles["Normal"], fontSize=8.5, leading=11)
    hcell_st = ParagraphStyle("hcell", parent=styles["Normal"], fontSize=9, leading=11,
                               fontName="Helvetica-Bold", textColor=WHITE)

    def wrap(text):
        safe = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return Paragraph(safe, cell_st)

    def wrap_h(text):
        return Paragraph(text, hcell_st)

    header_row = [wrap_h(h) for h in ["#", "Date", "Shift", "Shift Time", "Assignment"]]
    rows = [header_row]

    total_morning = total_evening = auto_count = 0

    for i, booking in enumerate(bookings, 1):
        slot      = booking.slot
        exam_date = slot.exam_date

        if slot.shift == "Morning":
            shift_time = f"{exam_date.morning_start.strftime('%H:%M')}–{exam_date.morning_end.strftime('%H:%M')}"
            total_morning += 1
            shift_bg = ParagraphStyle("sb", parent=cell_st, backColor=MORNING_C)
        else:
            shift_time = f"{exam_date.evening_start.strftime('%H:%M')}–{exam_date.evening_end.strftime('%H:%M')}"
            total_evening += 1
            shift_bg = ParagraphStyle("sb", parent=cell_st, backColor=EVENING_C)

        is_auto = booking.auto_assigned
        if is_auto:
            auto_count += 1

        asgn_st = ParagraphStyle("asgn", parent=cell_st,
                                  textColor=GREY if is_auto else GREEN,
                                  fontName="Helvetica-Bold")

        rows.append([
            wrap(str(i)),
            wrap(exam_date.date.strftime("%d %b %Y")),
            Paragraph(slot.shift, shift_bg),
            wrap(shift_time),
            Paragraph("Auto" if is_auto else "Self-booked", asgn_st),
        ])

    col_widths = [0.8*cm, 2.6*cm, 2.0*cm, 3.5*cm, 3.5*cm]
    tbl = Table(rows, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",     (0, 0), (-1, 0), PRIMARY),
        ("TEXTCOLOR",      (0, 0), (-1, 0), WHITE),
        ("FONTNAME",       (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BOTTOMPADDING",  (0, 0), (-1, 0), 8),
        ("TOPPADDING",     (0, 0), (-1, 0), 8),
        ("FONTNAME",       (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",       (0, 1), (-1, -1), 8.5),
        ("TOPPADDING",     (0, 1), (-1, -1), 5),
        ("BOTTOMPADDING",  (0, 1), (-1, -1), 5),
        ("LEFTPADDING",    (0, 0), (-1, -1), 7),
        ("RIGHTPADDING",   (0, 0), (-1, -1), 7),
        ("GRID",           (0, 0), (-1, -1), 0.4, colors.HexColor("#d0d0d0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, ROW_ALT]),
        ("VALIGN",         (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(tbl)

    # ── Summary ──
    story.append(Spacer(1, 0.5*cm))
    total       = len(bookings)
    self_booked = total - auto_count

    sum_hst = ParagraphStyle("sh", parent=styles["Normal"], fontSize=8.5,
                              fontName="Helvetica-Bold", textColor=WHITE, alignment=1)
    sum_vst = ParagraphStyle("sv", parent=styles["Normal"], fontSize=13,
                              fontName="Helvetica-Bold", textColor=PRIMARY, alignment=1)

    summary_data = [
        [Paragraph(h, sum_hst) for h in
         ["Total Duties", "Morning", "Evening", "Self-booked", "Auto-assigned"]],
        [Paragraph(str(v), sum_vst) for v in
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

    doc.build(story)
    buffer.seek(0)

    response = HttpResponse(buffer, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="my_duty_report_{teacher.faculty_id}.pdf"'
    )
    return response

# ─── Unavailability ─────────────────────────────────────────────────────────

@login_required
def add_unavailability(request):
    if request.method == 'POST':
        date_str = request.POST.get('date', '').strip()
        try:
            from datetime import datetime
            date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            messages.error(request, "Invalid date.")
            return redirect('dashboard')

        teacher = request.user.teacher

        # Block changes after booking deadline
        active_session = Session.objects.filter(status='active').first()
        if active_session:
            exam = ExamDate.objects.filter(session=active_session).first()
            if exam and timezone.now() > exam.booking_deadline:
                messages.error(request, "Cannot change unavailability after booking deadline.")
                return redirect('dashboard')

        _, created = TeacherUnavailability.objects.get_or_create(
            teacher=teacher, date=date
        )
        if created:
            messages.success(request, f"Marked unavailable on {date}.")
        else:
            messages.error(request, "Already marked unavailable on that date.")

    return redirect('dashboard')


@login_required
def remove_unavailability(request, unavailability_id):
    teacher = request.user.teacher
    TeacherUnavailability.objects.filter(
        id=unavailability_id, teacher=teacher
    ).delete()
    messages.success(request, "Unavailability removed.")
    return redirect('dashboard')

# ─── Swap Page ────────────────────────────────────────────────────────────────

@login_required
def swap_page(request):
    teacher = request.user.teacher

    active_session = Session.objects.filter(status='active').first()

    # My bookings in active session (to offer for swap)
    my_bookings = Booking.objects.filter(
        teacher=teacher,
        slot__exam_date__session=active_session
    ).select_related('slot__exam_date') if active_session else []

    # All other teachers' bookings (potential swap targets)
    other_bookings = Booking.objects.filter(
        slot__exam_date__session=active_session
    ).exclude(
        teacher=teacher
    ).select_related(
        'slot__exam_date', 'teacher__department'
    ).order_by(
        'slot__exam_date__date', 'slot__shift'
    ) if active_session else []

    # Filter out bookings that already have a pending swap request
    already_requested_target_ids = SwapRequest.objects.filter(
        requester=teacher,
        status__in=['pending', 'accepted']
    ).values_list('target_booking_id', flat=True)

    other_bookings = [b for b in other_bookings if b.id not in already_requested_target_ids]

    # Incoming swap requests (others want my slots)
    incoming = SwapRequest.objects.filter(
        target=teacher,
        status='pending'
    ).select_related(
        'requester', 'requester_booking__slot__exam_date',
        'target_booking__slot__exam_date'
    )

    # Outgoing swap requests (I requested)
    outgoing = SwapRequest.objects.filter(
        requester=teacher,
        status__in=['pending', 'accepted']
    ).select_related(
        'target', 'requester_booking__slot__exam_date',
        'target_booking__slot__exam_date'
    )

    # History (done/rejected/cancelled)
    history = SwapRequest.objects.filter(
        Q(requester=teacher) | Q(target=teacher),
        status__in=['approved', 'rejected', 'cancelled']
    ).select_related(
        'requester', 'target',
        'requester_booking__slot__exam_date',
        'target_booking__slot__exam_date'
    )[:10]

    deadline_passed = False
    if active_session:
        exam = ExamDate.objects.filter(session=active_session).first()
        if exam and timezone.now() > exam.booking_deadline:
            deadline_passed = True

    return render(request, 'swaps.html', {
        'teacher': teacher,
        'active_session': active_session,
        'my_bookings': my_bookings,
        'other_bookings': other_bookings,
        'incoming': incoming,
        'outgoing': outgoing,
        'history': history,
        'deadline_passed': deadline_passed,
    })


@login_required
def request_swap(request):
    if request.method != 'POST':
        return redirect('swap_page')

    teacher = request.user.teacher
    my_booking_id = request.POST.get('my_booking_id')
    target_booking_id = request.POST.get('target_booking_id')

    try:
        my_booking = Booking.objects.get(id=my_booking_id, teacher=teacher)
        target_booking = Booking.objects.get(id=target_booking_id)
    except Booking.DoesNotExist:
        messages.error(request, "Invalid booking selection.")
        return redirect('swap_page')

    if target_booking.teacher == teacher:
        messages.error(request, "Cannot swap with your own booking.")
        return redirect('swap_page')

    if timezone.now() > my_booking.slot.exam_date.booking_deadline:
        messages.error(request, "Cannot request swap after deadline.")
        return redirect('swap_page')

    # No duplicate pending swaps
    if SwapRequest.objects.filter(
        requester=teacher,
        requester_booking=my_booking,
        status__in=['pending', 'accepted']
    ).exists():
        messages.error(request, "You already have a pending swap for this booking.")
        return redirect('swap_page')

    SwapRequest.objects.create(
        requester=teacher,
        requester_booking=my_booking,
        target=target_booking.teacher,
        target_booking=target_booking,
    )

    Notification.objects.create(
        teacher=target_booking.teacher,
        activity='booked',
        message=f"{teacher.full_name} wants to swap their "
                f"{my_booking.slot.shift} shift on {my_booking.slot.exam_date.date} "
                f"with your {target_booking.slot.shift} shift on "
                f"{target_booking.slot.exam_date.date}. Go to Swap Requests to respond."
    )

    messages.success(request, "Swap request sent!")
    return redirect('swap_page')


@login_required
def respond_swap(request, swap_id, action):
    teacher = request.user.teacher
    swap = get_object_or_404(SwapRequest, id=swap_id, target=teacher, status='pending')

    if action == 'accept':
        swap.status = 'accepted'
        swap.save()
        Notification.objects.create(
            teacher=swap.requester,
            activity='booked',
            message=f"{teacher.full_name} accepted your swap request. "
                    f"Waiting for admin approval."
        )
        messages.success(request, "Swap accepted. Pending admin approval.")

    elif action == 'reject':
        swap.status = 'rejected'
        swap.save()
        Notification.objects.create(
            teacher=swap.requester,
            activity='cancelled',
            message=f"{teacher.full_name} rejected your swap request for "
                    f"{swap.requester_booking.slot.shift} on "
                    f"{swap.requester_booking.slot.exam_date.date}."
        )
        messages.success(request, "Swap request rejected.")

    return redirect('swap_page')


@login_required
def cancel_swap(request, swap_id):
    teacher = request.user.teacher
    swap = get_object_or_404(
        SwapRequest, id=swap_id, requester=teacher,
        status__in=['pending', 'accepted']
    )
    swap.status = 'cancelled'
    swap.save()
    messages.success(request, "Swap request cancelled.")
    return redirect('swap_page')