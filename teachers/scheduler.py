import logging
from django.utils import timezone
from django.db.models import F  
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from django_apscheduler.jobstores import DjangoJobStore
from django.db.models import Exists, OuterRef
from teachers.models import DeputySuperintendent

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler(timezone=timezone.get_current_timezone())

def auto_assign_duties():
    logger.info("Running auto_assign_duties...")

    from .models import (
        ExamDate, Slot, Booking, Teacher,
        Notification, Session, TeacherUnavailability
    )

    current_time = timezone.now()

    active_session = Session.objects.filter(status='active').first()
    if not active_session:
        logger.info("No active session found.")
        return

    expired_exams = ExamDate.objects.filter(
        session=active_session,
        booking_deadline__lt=current_time
    )

    # PRELOAD AVAILABILITY
    unavailability_map = {}

    for entry in TeacherUnavailability.objects.all():
        t_id = entry.teacher_id
        date = entry.date

        if t_id not in unavailability_map:
            unavailability_map[t_id] = set()

        unavailability_map[t_id].add(date)

    for exam in expired_exams:
        open_slots = list(Slot.objects.filter(
            exam_date=exam,
            filled_positions__lt=F('total_positions')
        ))

        if not open_slots:
            continue

        teachers = list(Teacher.objects.filter(
            duties__gt=0
        ).filter(
            ~Exists(DeputySuperintendent.objects.filter(teacher=OuterRef('pk')))
        ))
        if not teachers:
            continue

        # PRELOAD BOOKINGS
        all_bookings = Booking.objects.filter(
            slot__exam_date__session=active_session
        ).select_related('slot__exam_date')

        teacher_booking_count = {}
        teacher_day_count = {}
        teacher_day_shifts = {}

        for booking in all_bookings:
            t_id = booking.teacher_id
            date = booking.slot.exam_date.date
            shift = booking.slot.shift

            teacher_booking_count[t_id] = teacher_booking_count.get(t_id, 0) + 1

            if t_id not in teacher_day_count:
                teacher_day_count[t_id] = {}
            teacher_day_count[t_id][date] = teacher_day_count[t_id].get(date, 0) + 1

            if t_id not in teacher_day_shifts:
                teacher_day_shifts[t_id] = {}
            if date not in teacher_day_shifts[t_id]:
                teacher_day_shifts[t_id][date] = set()
            teacher_day_shifts[t_id][date].add(shift)

        # ASSIGN PER SLOT
        for slot in open_slots:
            available = slot.total_positions - slot.filled_positions
            if available <= 0:
                continue

            scored_teachers = []

            for teacher in teachers:
                if teacher.duties <= 0:
                    continue

                t_id = teacher.teacher_id
                date = slot.exam_date.date
                shift = slot.shift

                # already booked same slot
                if Booking.objects.filter(teacher=teacher, slot=slot).exists():
                    continue

                # HARD RULE: unavailability
                if date in unavailability_map.get(t_id, set()):
                    continue

                # tracking
                day_count = teacher_day_count.get(t_id, {}).get(date, 0)
                day_shifts = teacher_day_shifts.get(t_id, {}).get(date, set())

                # max 2 duties per day
                if day_count >= 2:
                    continue

                # same shift twice
                if shift in day_shifts:
                    continue

                score = 0

                # 1. Duties priority
                score += teacher.duties * 10

                # 2. Workload balance
                bookings_done = teacher_booking_count.get(t_id, 0)
                score -= bookings_done * 5

                # 3. Slight penalty if already 1 duty that day
                if day_count == 1:
                    score -= 20

                # 4. Relaxation penalty
                score -= teacher.relaxation_duties * 2

                # 🟢 SOFT RULE: preferred shift
                if teacher.preferred_shift == shift:
                    score += 15
                elif teacher.preferred_shift != 'none':
                    score -= 5

                scored_teachers.append((score, teacher))

            # sort by best score
            scored_teachers.sort(reverse=True, key=lambda x: x[0])

            # ASSIGN
            for score, teacher in scored_teachers:
                if available <= 0:
                    break

                if teacher.duties <= 0:
                    continue

                t_id = teacher.teacher_id
                date = slot.exam_date.date
                shift = slot.shift

                Booking.objects.create(
                    teacher=teacher,
                    slot=slot,
                    auto_assigned=True
                )

                slot.filled_positions += 1
                slot.save()

                teacher.duties -= 1
                teacher.save()

                # update tracking
                teacher_booking_count[t_id] = teacher_booking_count.get(t_id, 0) + 1

                if t_id not in teacher_day_count:
                    teacher_day_count[t_id] = {}
                teacher_day_count[t_id][date] = teacher_day_count[t_id].get(date, 0) + 1

                if t_id not in teacher_day_shifts:
                    teacher_day_shifts[t_id] = {}
                if date not in teacher_day_shifts[t_id]:
                    teacher_day_shifts[t_id][date] = set()
                teacher_day_shifts[t_id][date].add(shift)

                Notification.objects.create(
                    teacher=teacher,
                    activity='auto_assigned',
                    message=f"You were auto-assigned to {shift} shift on {date}."
                )

                available -= 1

    active_session.status = 'completed'
    active_session.save()

    logger.info("Smart auto assignment completed.")


def schedule_auto_assignment(deadline):
    from django.utils.timezone import is_naive, make_aware
    # Make sure deadline is timezone-aware
    if is_naive(deadline):
        deadline = make_aware(deadline)

    scheduler.add_job(
        auto_assign_duties,
        trigger=DateTrigger(run_date=deadline),
        id='auto_assign_duties',
        name='Auto assign after deadline',
        replace_existing=True,
        jobstore='default'  # ← persists in DB so survives restarts
    )
    logger.info(f"Auto assignment scheduled for {deadline}")


def start():
    scheduler.add_jobstore(DjangoJobStore(), "default")
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started.")