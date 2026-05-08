import csv
import io
import math
from django.template.response import TemplateResponse
from django.contrib import admin
from django.contrib import messages
from django.contrib.auth.models import User
from django.shortcuts import render, redirect
from django.urls import path
from django.utils.html import format_html
from django.utils.dateparse import parse_datetime
from django import forms
from .models import Department, Exam, SwapRequest, Teacher, Slot, ExamDate, Booking, Notification, Session, TeacherUnavailability, DeputySuperintendent
from django.http import HttpResponse
from django.urls import reverse
from django.utils.safestring import mark_safe


# ─── Department Admin ──────────────────────────────────────────────────────────

class TeacherInlineDepartment(admin.TabularInline):
    model = Teacher
    fields = ['faculty_id', 'full_name', 'email', 'preferred_shift', 'duties']
    readonly_fields = ['faculty_id', 'full_name', 'email', 'preferred_shift', 'duties']
    extra = 0  # don't show empty extra rows
    can_delete = False
    show_change_link = True  # clicking takes you to the teacher's full page

    def has_add_permission(self, request, obj=None):
        return False  # can't add teachers from here, only view


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ['name', 'teacher_count']
    search_fields = ['name']
    inlines = [TeacherInlineDepartment]

    def teacher_count(self, obj):
        return obj.teacher_set.count()
    teacher_count.short_description = 'Teachers'


# ─── Duty Calculation Action ───────────────────────────────────────────────────

def calculate_duties(modeladmin, request, queryset):
    from django.db.models import Exists, OuterRef
    teachers = list(Teacher.objects.filter(
        ~Exists(DeputySuperintendent.objects.filter(teacher=OuterRef('pk')))
    ))
    
    if not teachers:
        modeladmin.message_user(request, "No teachers found.", level=messages.ERROR)
        return

    # FIX: Call save() to recalculate relaxation_duties from relaxation_hours
    for teacher in teachers:
        teacher.save()

    active_session = Session.objects.filter(status='active').first()
    if not active_session:
        modeladmin.message_user(request, "No active session found.", level=messages.ERROR)
        return

    # FIX: Count total individual slot positions (each Slot has total_positions=1),
    # so total_slots = number of Slot objects = total duty positions to fill.
    total_slots = Slot.objects.filter(
        exam_date__session=active_session
    ).count()

    if total_slots <= 0:
        modeladmin.message_user(request, "No slots found in active session.", level=messages.ERROR)
        return

    total_relaxation = sum(t.relaxation_duties for t in teachers)
    non_relaxed = [t for t in teachers if t.relaxation_duties == 0]

    if not non_relaxed:
        modeladmin.message_user(request, "Warning: All teachers have relaxation.", level=messages.WARNING)
        return

    # FIX: Corrected duty distribution algorithm.
    # Each teacher gets base_duty minus their relaxation.
    # The duties freed up by relaxation + remainder are redistributed to non-relaxed teachers.
    base_duty = total_slots // len(teachers)
    remainder = total_slots % len(teachers)

    for teacher in teachers:
        teacher.duties = max(0, base_duty - teacher.relaxation_duties)

    extra = total_relaxation + remainder
    i = 0
    while extra > 0:
        non_relaxed[i % len(non_relaxed)].duties += 1
        extra -= 1
        i += 1

    for teacher in teachers:
        teacher.save()

    modeladmin.message_user(
        request,
        f"Duties calculated! Total slots: {total_slots}, distributed across {len(teachers)} teachers.",
        level=messages.SUCCESS
    )

calculate_duties.short_description = "Calculate & distribute duties across all teachers"


# ─── Teacher Admin Forms ───────────────────────────────────────────────────────

class TeacherCreationForm(forms.ModelForm):
    username = forms.CharField(max_length=150)
    password1 = forms.CharField(label='Password', widget=forms.PasswordInput)
    password2 = forms.CharField(label='Confirm Password', widget=forms.PasswordInput)

    class Meta:
        model = Teacher
        fields = [
            'faculty_id',
            'full_name',
            'department',
            'phone_number',
            'email',          # FIX: email is needed for OTP-based password reset
            'preferred_shift',
            'photo',
            'relaxation_hours'
        ]

    def clean(self):
        cleaned_data = super().clean()
        p1 = cleaned_data.get('password1')
        p2 = cleaned_data.get('password2')

        if p1 and p2 and p1 != p2:
            raise forms.ValidationError("Passwords don't match.")

        return cleaned_data

    def clean_username(self):
        username = self.cleaned_data.get('username')
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError("Username already exists.")
        return username

    def clean_faculty_id(self):
        faculty_id = self.cleaned_data.get('faculty_id')
        if Teacher.objects.filter(faculty_id=faculty_id).exists():
            raise forms.ValidationError("Faculty ID already exists.")
        return faculty_id


class TeacherChangeForm(forms.ModelForm):
    class Meta:
        model = Teacher
        fields = [
            'faculty_id',
            'full_name',
            'department',
            'phone_number',
            'email',          # FIX: must be editable so OTP works after edit too
            'preferred_shift',
            'photo',
            'relaxation_hours'
        ]


# ─── Teacher Unavailability Inline ────────────────────────────────────────────

class TeacherUnavailabilityInline(admin.TabularInline):
    model = TeacherUnavailability
    extra = 1


# ─── Teacher Admin ─────────────────────────────────────────────────────────────

@admin.register(Teacher)
class TeacherAdmin(admin.ModelAdmin):
    list_display = [
        'faculty_id',
        'full_name',
        'email',
        'department',
        'preferred_shift',
        'relaxation_hours',
        'relaxation_duties',
        'duties',
        'get_username',
        'is_deputy_badge'
    ]

    search_fields = ['faculty_id', 'full_name', 'email']

    inlines = [TeacherUnavailabilityInline]
    readonly_fields = ['relaxation_duties']
    actions = [calculate_duties]

    def get_username(self, obj):
        return obj.user.username
    get_username.short_description = 'Username'

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                'bulk-upload/',
                self.admin_site.admin_view(self.bulk_upload_view),
                name='teachers_teacher_bulk_upload'
            ),
            path(
                'unavailability-upload/',
                self.admin_site.admin_view(self.unavailability_upload_view),  # FIX: lowercase method name
                name='teachers_teacher_unavailability_upload'
            ),
            path(
                'download-csv/',
                self.admin_site.admin_view(self.download_teachers_csv),
                name='teachers_teacher_download_teachers_csv'  # ← Django prefixes with app_model
            ),
        ]
        return custom_urls + urls

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context['bulk_upload_url'] = reverse('admin:teachers_teacher_bulk_upload')
        extra_context['unavailability_upload_url'] = reverse('admin:teachers_teacher_unavailability_upload')
        extra_context['download_csv_url'] = reverse('admin:teachers_teacher_download_teachers_csv')
        return super().changelist_view(request, extra_context=extra_context)

    def bulk_upload_view(self, request):
        if request.method == 'POST':
            csv_file = request.FILES.get('csv_file')
 
            if not csv_file:
                self.message_user(request, "Please upload a CSV file.", level=messages.ERROR)
                return redirect(request.path)
 
            decoded = csv_file.read().decode('utf-8')
            reader = csv.DictReader(io.StringIO(decoded))
 
            rows = []
 
            for i, row in enumerate(reader, start=2):
                faculty_id = (row.get('faculty_id') or '').strip()
                full_name = (row.get('full_name') or '').strip()
                department_name = (row.get('department') or '').strip()
                phone_number = (row.get('phone_number') or '').strip()
                email = (row.get('email') or '').strip()
                relaxation_hours = (row.get('relaxation_hours') or '0').strip()
                preferred_shift = (row.get('preferred_shift') or 'none').strip().lower()
 
                if not faculty_id:
                    self.message_user(request, f"Row {i}: faculty_id is required.", level=messages.ERROR)
                    return redirect(request.path)
 
                if not full_name:
                    self.message_user(request, f"Row {i}: full_name is required.", level=messages.ERROR)
                    return redirect(request.path)
 
                if not email:
                    self.message_user(request, f"Row {i}: email is required.", level=messages.ERROR)
                    return redirect(request.path)
 
                # ── REMOVED: faculty_id duplicate rejection ──
                # We now update existing teachers instead of rejecting them.
 
                try:
                    relaxation_hours = int(relaxation_hours)
                except ValueError:
                    self.message_user(
                        request,
                        f"Row {i}: relaxation_hours must be a number.",
                        level=messages.ERROR
                    )
                    return redirect(request.path)
 
                if preferred_shift not in ['morning', 'evening', 'none']:
                    preferred_shift = 'none'
 
                parts = full_name.split()
                first_name = parts[0]
                last_name = parts[-1] if len(parts) > 1 else ''
 
                department = None
                if department_name:
                    department, _ = Department.objects.get_or_create(name=department_name)
 
                if last_name:
                    password = f"{first_name.lower()}.{last_name.lower()}@123"
                else:
                    password = f"{first_name.lower()}@123"
 
                rows.append({
                    'faculty_id': faculty_id,
                    'full_name': full_name,
                    'first_name': first_name,
                    'last_name': last_name,
                    'phone_number': phone_number,
                    'email': email,
                    'relaxation_hours': relaxation_hours,
                    'preferred_shift': preferred_shift,
                    'department': department,
                    'password': password,
                })
 
            if not rows:
                self.message_user(request, "CSV file is empty.", level=messages.ERROR)
                return redirect(request.path)
 
            created_count = 0
            updated_count = 0
            teacher_credentials = []
 
            for row in rows:
                base_username = (row['first_name'] + row['last_name']).lower() or "teacher"
 
                existing = Teacher.objects.filter(faculty_id=row['faculty_id']).first()
 
                if existing:
                    existing.full_name        = row['full_name']
                    existing.first_name       = row['first_name']
                    existing.last_name        = row['last_name']
                    existing.phone_number     = row['phone_number']
                    existing.email            = row['email']
                    existing.relaxation_hours = row['relaxation_hours']
                    existing.preferred_shift  = row['preferred_shift']
                    existing.department       = row['department']
                    existing.save()
                    updated_count += 1
 
                    teacher_credentials.append({
                        'name':       row['full_name'],
                        'faculty_id': row['faculty_id'],
                        'department': row['department'].name if row['department'] else '',
                        'username':   existing.user.username,
                        'password':   '(unchanged)',
                    })
 
                else:
                    username = base_username
                    counter = 1
                    while User.objects.filter(username=username).exists():
                        username = f"{base_username}{counter}"
                        counter += 1
 
                    user = User.objects.create_user(
                        username=username,
                        password=row['password'],
                    )
 
                    Teacher.objects.create(
                        user=user,
                        faculty_id=row['faculty_id'],
                        full_name=row['full_name'],
                        first_name=row['first_name'],
                        last_name=row['last_name'],
                        phone_number=row['phone_number'],
                        email=row['email'],
                        relaxation_hours=row['relaxation_hours'],
                        preferred_shift=row['preferred_shift'],
                        department=row['department'],
                    )
 
                    created_count += 1
 
                    teacher_credentials.append({
                        'name':       row['full_name'],
                        'faculty_id': row['faculty_id'],
                        'department': row['department'].name if row['department'] else '',
                        'username':   username,
                        'password':   row['password'],
                    })
 
            self.message_user(
                request,
                f"{created_count} teacher(s) created, {updated_count} updated.",
                level=messages.SUCCESS,
            )
 
            request.session['last_upload_credentials'] = teacher_credentials
 
            return TemplateResponse(request, 'admin/teacher_upload_success.html', {
                'title': 'Bulk Upload Complete',
                'opts':  Teacher._meta,
                'teachers': teacher_credentials,
            })
 
        return TemplateResponse(request, 'admin/teacher_bulk_upload.html', {
            'title': 'Bulk Upload Teachers',
            'opts': Teacher._meta,
        })


    def unavailability_upload_view(self, request):
        from datetime import datetime

        if request.method == 'POST':
            csv_file = request.FILES.get('csv_file')

            if not csv_file:
                self.message_user(request, "Upload a CSV file.", level=messages.ERROR)
                return redirect(request.path)

            decoded = csv_file.read().decode('utf-8')
            reader = csv.DictReader(io.StringIO(decoded))

            created = 0
            skipped = 0

            for i, row in enumerate(reader, start=2):
                email = (row.get('email') or '').strip()
                date_str = (row.get('date') or '').strip()

                if not email or not date_str:
                    self.message_user(
                        request,
                        f"Row {i}: email and date are required.",
                        level=messages.ERROR
                    )
                    return redirect(request.path)

                try:
                    date = datetime.strptime(date_str, "%Y-%m-%d").date()
                except ValueError:
                    self.message_user(
                        request,
                        f"Row {i}: Invalid date format (use YYYY-MM-DD).",
                        level=messages.ERROR
                    )
                    return redirect(request.path)

                try:
                    teacher = Teacher.objects.get(email=email)
                except Teacher.DoesNotExist:
                    self.message_user(
                        request,
                        f"Row {i}: Teacher with email '{email}' not found.",
                        level=messages.ERROR
                    )
                    return redirect(request.path)

                obj, created_flag = TeacherUnavailability.objects.get_or_create(
                    teacher=teacher,
                    date=date
                )

                if created_flag:
                    created += 1
                else:
                    skipped += 1

            self.message_user(
                request,
                f"{created} unavailability records added, {skipped} already existed.",
                level=messages.SUCCESS
            )

            return redirect('admin:index')

        return TemplateResponse(request, 'admin/teacher_unavailability_upload.html', {
            'title': 'Upload Teacher Unavailability',
            'opts': Teacher._meta,
        })

    def download_teachers_csv(self, request):
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="teachers_credentials.csv"'

        writer = csv.writer(response)
        writer.writerow(['Faculty ID', 'Full Name', 'Department', 'Username', 'Password'])

        # Use session credentials if available (right after bulk upload)
        credentials = request.session.pop('last_upload_credentials', None)

        if credentials:
            for t in credentials:
                writer.writerow([
                    t['faculty_id'],
                    t['name'],
                    t['department'],
                    t['username'],
                    t['password'],   # plain text, from memory before hashing
                ])
        else:
            # Fallback: download without passwords (already hashed in DB)
            teachers = Teacher.objects.select_related('user', 'department').all()
            for t in teachers:
                writer.writerow([
                    t.faculty_id,
                    t.full_name,
                    t.department.name if t.department else '',
                    t.user.username,
                    '** password not available **',
                ])

        return response

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            return TeacherCreationForm
        return TeacherChangeForm

    def save_model(self, request, obj, form, change):
        if not change:
            user = User.objects.create_user(
                username=form.cleaned_data['username'],
                password=form.cleaned_data['password1'],
            )
            obj.user = user
        obj.save()

    def delete_model(self, request, obj):
        user = obj.user
        obj.delete()
        user.delete()

    def delete_queryset(self, request, queryset):
        for teacher in queryset:
            user = teacher.user
            teacher.delete()
            user.delete()

    def is_deputy_badge(self, obj):
        if hasattr(obj, 'deputy'):
            return format_html(
                '<span style="color:white;background:#e65100;padding:2px 8px;'
                'border-radius:10px;font-size:11px;">{}</span>',
                'Deputy Superintendent'   # no emoji, plain text as the {} argument
            )
        return '-'
    is_deputy_badge.short_description = 'Role'


# ─── Session Admin with CSV Upload ────────────────────────────────────────────

@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = ['name', 'status', 'created_at', 'setup_link']
    readonly_fields = ['created_at']

    def setup_link(self, obj):
        return format_html(
            '<a class="button" href="{}">📂 Setup Session</a>',
            f'/admin/teachers/session/{obj.pk}/setup/'
        )
    setup_link.short_description = 'Setup'

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                '<int:session_id>/setup/',
                self.admin_site.admin_view(self.setup_session_view),
                name='session_setup'
            ),
        ]
        return custom_urls + urls
    
    def has_delete_permission(self, request, obj=None):
        return True

    def setup_session_view(self, request, session_id):
        session = Session.objects.get(pk=session_id)

        if request.method == 'POST':
            csv_file = request.FILES.get('csv_file')
            booking_deadline_raw = request.POST.get('booking_deadline')
            buffer = request.POST.get('buffer', '0').strip()
            morning_start = request.POST.get('morning_start')
            morning_end = request.POST.get('morning_end')
            evening_start = request.POST.get('evening_start')
            evening_end = request.POST.get('evening_end')

            from datetime import datetime

            def parse_time(value):
                for fmt in ("%H:%M", "%H:%M:%S"):
                    try:
                        return datetime.strptime(value, fmt).time()
                    except Exception:
                        continue
                return None

            morning_start = parse_time(morning_start)
            morning_end = parse_time(morning_end)
            evening_start = parse_time(evening_start)
            evening_end = parse_time(evening_end)

            if not all([morning_start, morning_end, evening_start, evening_end]):
                self.message_user(request, "Invalid time format.", level=messages.ERROR)
                return redirect(request.path)

            booking_deadline_dt = parse_datetime(booking_deadline_raw)
            if not booking_deadline_dt:
                self.message_user(request, "Invalid booking deadline format.", level=messages.ERROR)
                return redirect(request.path)

            from django.utils import timezone
            if timezone.is_naive(booking_deadline_dt):
                booking_deadline_dt = timezone.make_aware(booking_deadline_dt)

            try:
                buffer = int(buffer)
            except Exception:
                buffer = 0

            if not csv_file:
                self.message_user(request, "Upload CSV file.", level=messages.ERROR)
                return redirect(request.path)

            decoded = csv_file.read().decode('utf-8')
            reader = csv.DictReader(io.StringIO(decoded))

            from collections import defaultdict
            data = defaultdict(list)

            for i, row in enumerate(reader, start=2):
                exam_name        = (row.get('exam_name')        or '').strip()
                date_str         = (row.get('exam_date')        or '').strip()
                total_students   = (row.get('total_students')   or '').strip()
                morning_students = (row.get('morning_students') or '0').strip()
                evening_students = (row.get('evening_students') or '0').strip()

                if not all([exam_name, date_str, total_students]):
                    self.message_user(request, f"Row {i}: Missing data.", level=messages.ERROR)
                    return redirect(request.path)

                try:
                    total_students   = int(total_students)
                    morning_students = int(morning_students)
                    evening_students = int(evening_students)
                    date = datetime.strptime(date_str, "%Y-%m-%d").date()
                except Exception:
                    self.message_user(request, f"Row {i}: Invalid data.", level=messages.ERROR)
                    return redirect(request.path)

                if morning_students == 0 and evening_students == 0:
                    self.message_user(request, f"Row {i}: Both morning and evening students cannot be 0.", level=messages.ERROR)
                    return redirect(request.path)

                data[date].append({
                    'exam_name':        exam_name,
                    'total_students':   total_students,
                    'morning_students': morning_students,
                    'evening_students': evening_students,
                })

            created_dates = 0
            created_slots = 0

            for date, exams in data.items():
                total_students         = sum(e['total_students']   for e in exams)
                total_morning_students = sum(e['morning_students'] for e in exams)
                total_evening_students = sum(e['evening_students'] for e in exams)

                exam_date, created = ExamDate.objects.get_or_create(
                    session=session,
                    date=date,
                    defaults={
                        'total_students':   total_students,
                        'booking_deadline': booking_deadline_dt,
                        'morning_start':    morning_start,
                        'morning_end':      morning_end,
                        'evening_start':    evening_start,
                        'evening_end':      evening_end,
                    }
                )

                if created:
                    created_dates += 1
                else:
                    exam_date.total_students   = total_students
                    exam_date.booking_deadline = booking_deadline_dt
                    exam_date.morning_start    = morning_start
                    exam_date.morning_end      = morning_end
                    exam_date.evening_start    = evening_start
                    exam_date.evening_end      = evening_end
                    exam_date.save()

                    exam_date.exams.all().delete()
                    exam_date.slots.all().delete()

                # ── Create Exams ──
                for e in exams:
                    if e['morning_students'] > 0:
                        Exam.objects.create(
                            exam_date=exam_date,
                            exam_name=e['exam_name'],
                            students=e['morning_students'],
                            session='Morning'
                        )
                    if e['evening_students'] > 0:
                        Exam.objects.create(
                            exam_date=exam_date,
                            exam_name=e['exam_name'],
                            students=e['evening_students'],
                            session='Evening'
                        )

                # ── Create Slots ──
                if total_morning_students > 0:
                    morning_positions = math.ceil(total_morning_students / 50) * 2 + buffer
                    Slot.objects.create(
                        exam_date=exam_date,
                        shift='Morning',
                        total_positions=morning_positions,
                        filled_positions=0
                    )
                    created_slots += morning_positions

                if total_evening_students > 0:
                    evening_positions = math.ceil(total_evening_students / 50) * 2 + buffer
                    Slot.objects.create(
                        exam_date=exam_date,
                        shift='Evening',
                        total_positions=evening_positions,
                        filled_positions=0
                    )
                    created_slots += evening_positions

            # ── Activate session ──
            if not Session.objects.filter(status='active').exists():
                session.status = 'active'
                session.save()

            # ── Schedule auto assignment ──
            from .scheduler import schedule_auto_assignment
            schedule_auto_assignment(booking_deadline_dt)

            self.message_user(
                request,
                f"Setup complete! {created_dates} exam dates created, {created_slots} total invigilator positions.",
                level=messages.SUCCESS
            )

            return redirect('/admin/teachers/session/')

        return render(request, 'admin/session_setup.html', {
            'session': session,
            'title': f'Setup Session: {session.name}',
            'opts': self.model._meta,
        })
    



# ─── ExamDate Admin ────────────────────────────────────────────────────────────

@admin.register(ExamDate)
class ExamDateAdmin(admin.ModelAdmin):
    list_display = ['date', 'session', 'total_students', 'rooms', 'slots_count', 'booking_deadline']
    readonly_fields = ['rooms', 'slots_count']
    list_filter = ['session']


# ─── Slot Admin ────────────────────────────────────────────────────────────────

@admin.register(Slot)
class SlotAdmin(admin.ModelAdmin):
    list_display = ['exam_date', 'shift', 'shift_students', 'total_positions', 'filled_positions', 'get_exams']
    readonly_fields = ['exam_date', 'shift', 'total_positions', 'filled_positions']
    list_filter = ['exam_date__session']

    def shift_students(self, obj):
        return obj.shift_students
    shift_students.short_description = 'Students (this shift)'


    def get_exams(self, obj):
        exams = obj.exam_date.exams.filter(session=obj.shift)
        names = [e.exam_name for e in exams]
        return mark_safe("<br>".join(names))
    get_exams.short_description = "Exams"

    def has_add_permission(self, request): return False
    
    def has_change_permission(self, request, obj=None):
        if obj: return False
        return True



# ─── Notification Admin ────────────────────────────────────────────────────────

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ['teacher', 'activity', 'message', 'is_read', 'created_at']
    list_filter = ['activity', 'is_read']


# ─── Booking Admin ─────────────────────────────────────────────────────────────

@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ['teacher', 'slot', 'booked_at', 'auto_assigned']
    list_filter = ['auto_assigned']


# ─── Teacher Unavailability Admin ─────────────────────────────────────────────

@admin.register(TeacherUnavailability)
class TeacherUnavailabilityAdmin(admin.ModelAdmin):
    list_display = ['teacher', 'date']
    list_filter  = ['date']
    search_fields = [
        'teacher__full_name',
        'teacher__faculty_id',
        'teacher__email',
    ]
 
    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                'bulk-upload/',
                self.admin_site.admin_view(self.bulk_upload_view),
                name='teachers_teacherunavailability_bulk_upload',
            ),
        ]
        return custom_urls + urls
 
    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context['bulk_upload_url'] = reverse(
            'admin:teachers_teacherunavailability_bulk_upload'
        )
        return super().changelist_view(request, extra_context=extra_context)
 
    def bulk_upload_view(self, request):
        import csv
        import io
        from datetime import datetime
        from django.template.response import TemplateResponse
 
        if request.method == 'POST':
            csv_file = request.FILES.get('csv_file')
 
            if not csv_file:
                self.message_user(request, "Please upload a CSV file.", level=messages.ERROR)
                return redirect(request.path)
 
            decoded = csv_file.read().decode('utf-8')
            reader  = csv.DictReader(io.StringIO(decoded))
 
            created = 0
            skipped = 0
 
            for i, row in enumerate(reader, start=2):
                faculty_id = (row.get('faculty_id') or '').strip()
                date_str   = (row.get('date')        or '').strip()
 
                if not faculty_id or not date_str:
                    self.message_user(
                        request,
                        f"Row {i}: both 'faculty_id' and 'date' are required.",
                        level=messages.ERROR,
                    )
                    return redirect(request.path)
 
                try:
                    date = datetime.strptime(date_str, "%Y-%m-%d").date()
                except ValueError:
                    self.message_user(
                        request,
                        f"Row {i}: invalid date format — use YYYY-MM-DD.",
                        level=messages.ERROR,
                    )
                    return redirect(request.path)
 
                try:
                    teacher = Teacher.objects.get(faculty_id=faculty_id)
                except Teacher.DoesNotExist:
                    self.message_user(
                        request,
                        f"Row {i}: no teacher found with faculty_id '{faculty_id}'.",
                        level=messages.ERROR,
                    )
                    return redirect(request.path)
 
                _, was_created = TeacherUnavailability.objects.get_or_create(
                    teacher=teacher,
                    date=date,
                )
                if was_created:
                    created += 1
                else:
                    skipped += 1
 
            self.message_user(
                request,
                f"{created} record(s) added, {skipped} already existed.",
                level=messages.SUCCESS,
            )
            return redirect('admin:teachers_teacherunavailability_changelist')
 
        return TemplateResponse(request, 'admin/teacher_unavailability_upload.html', {
            'title': 'Bulk Upload Teacher Unavailability',
            'opts':  self.model._meta,
        })
 

# ─── Deputy Superintendent Admin ─────────────────────────────────────────────

@admin.register(DeputySuperintendent)
class DeputySuperintendentAdmin(admin.ModelAdmin):
    list_display = ['teacher', 'get_department', 'assigned_at', 'assigned_by']
    search_fields = ['teacher__full_name', 'teacher__faculty_id']

    def get_department(self, obj):
        return obj.teacher.department
    get_department.short_description = 'Department'

    def save_model(self, request, obj, form, change):
        if not change:
            obj.assigned_by = request.user
            super().save_model(request, obj, form, change)

            # Auto-cancel any existing bookings for this teacher
            existing_bookings = Booking.objects.filter(teacher=obj.teacher)
            for booking in existing_bookings:
                slot = booking.slot
                slot.filled_positions -= 1
                slot.save()
            existing_bookings.delete()

            # Reset duties to 0 since deputies don't have duties
            obj.teacher.duties = 0
            obj.teacher.save()

            Notification.objects.create(
                teacher=obj.teacher,
                activity='cancelled',
                message="Your bookings have been cancelled. You have been assigned as Deputy Superintendent."
            )
        else:
            super().save_model(request, obj, form, change)

# ─── Swap Request Admin ─────────────────────────────────────────────────────

@admin.register(SwapRequest)
class SwapRequestAdmin(admin.ModelAdmin):
    list_display = ['requester', 'requester_booking', 'target', 'target_booking', 'status', 'created_at']
    list_filter = ['status']
    actions = ['approve_swaps', 'reject_swaps']

    def approve_swaps(self, request, queryset):
        for swap in queryset.filter(status='accepted'):
            b1 = swap.requester_booking
            b2 = swap.target_booking

            # Swap the slots between the two bookings
            b1_slot = b1.slot
            b1.slot = b2.slot
            b2.slot = b1_slot
            b1.save()
            b2.save()

            swap.status = 'approved'
            swap.save()

            Notification.objects.create(
                teacher=swap.requester,
                activity='booked',
                message=f"Swap approved! You now have "
                        f"{b1.slot.shift} on {b1.slot.exam_date.date}."
            )
            Notification.objects.create(
                teacher=swap.target,
                activity='booked',
                message=f"Swap approved! You now have "
                        f"{b2.slot.shift} on {b2.slot.exam_date.date}."
            )
    approve_swaps.short_description = "✅ Approve selected swaps (must be accepted by target first)"

    def reject_swaps(self, request, queryset):
        for swap in queryset.filter(status__in=['pending', 'accepted']):
            swap.status = 'rejected'
            swap.save()
            Notification.objects.create(
                teacher=swap.requester,
                activity='cancelled',
                message=f"Your swap request was rejected by admin."
            )
    reject_swaps.short_description = "❌ Reject selected swaps"