import math
from django.db import models
from django.contrib.auth.models import User


class Session(models.Model):
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('upcoming', 'Upcoming'),
        ('completed', 'Completed'),
    ]
    name = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='upcoming')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.status})"


class Department(models.Model):
    name = models.CharField(max_length=100, unique=True)

    def __str__(self):
        return self.name


class Teacher(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    teacher_id = models.AutoField(primary_key=True)
    faculty_id = models.CharField(max_length=50, unique=True)
    full_name = models.CharField(max_length=255)

    # NOTE: first_name and last_name are kept only because bulk_upload splits
    # full_name into them for legacy reasons. They are not displayed anywhere.
    # Consider a future migration to remove them entirely.
    first_name = models.CharField(max_length=100, blank=True, null=True)
    last_name = models.CharField(max_length=100, blank=True, null=True)

    department = models.ForeignKey(Department, on_delete=models.SET_NULL, null=True, blank=True)
    phone_number = models.CharField(max_length=12, blank=True)

    # Separate from User.email — used for OTP delivery and unavailability CSV lookup
    email = models.EmailField(blank=True)

    photo = models.ImageField(upload_to='teacher_photos/', blank=True, null=True)
    relaxation_hours = models.IntegerField(default=0)
    relaxation_duties = models.IntegerField(default=0)
    duties = models.IntegerField(default=0)

    otp = models.CharField(max_length=6, blank=True, null=True)
    otp_expiry = models.DateTimeField(blank=True, null=True)

    SHIFT_CHOICES = [
        ('morning', 'Morning'),
        ('evening', 'Evening'),
        ('none', 'No Preference'),
    ]
    preferred_shift = models.CharField(
        max_length=10,
        choices=SHIFT_CHOICES,
        default='none'
    )

    def save(self, *args, **kwargs):
        # Always recalculate relaxation_duties from relaxation_hours on every save
        self.relaxation_duties = self.relaxation_hours // 3
        # REMOVED: dead fallback that set full_name from first_name+last_name.
        # full_name is always set explicitly — from TeacherCreationForm,
        # TeacherChangeForm, or bulk_upload_view — so this branch never triggered.
        super().save(*args, **kwargs)

    def __str__(self):
        return self.full_name


class ExamDate(models.Model):
    session = models.ForeignKey(
        Session, on_delete=models.CASCADE,
        related_name='exam_dates', null=True, blank=True
    )
    date = models.DateField()
    morning_start = models.TimeField()
    morning_end = models.TimeField()
    evening_start = models.TimeField()
    evening_end = models.TimeField()
    total_students = models.IntegerField(default=0)
    booking_deadline = models.DateTimeField()

    @property
    def rooms(self):
        return math.ceil(self.total_students / 50)

    def slots_count(self):
        return self.slots.count()
    slots_count.short_description = "Slots"

    def __str__(self):
        return f"{self.date}"


class Exam(models.Model):
    exam_date = models.ForeignKey(ExamDate, on_delete=models.CASCADE, related_name='exams')
    exam_name = models.CharField(max_length=255)
   
    students = models.IntegerField()
    session = models.CharField(max_length=10)  # 'Morning' or 'Evening'

    def __str__(self):
        return f"{self.exam_name} ({self.session})"


class Slot(models.Model):
    exam_date = models.ForeignKey(ExamDate, related_name='slots', on_delete=models.CASCADE)
    shift = models.CharField(max_length=10)
    total_positions = models.IntegerField()
    filled_positions = models.IntegerField(default=0)

    @property
    def shift_students(self):
        return self.exam_date.exams.filter(session=self.shift).aggregate(
            total=models.Sum('students')
        )['total'] or 0
    
    class Meta:
        unique_together = ('exam_date', 'shift')

    def __str__(self):
        return f"{self.exam_date} - {self.shift}"


class Booking(models.Model):
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE)
    slot = models.ForeignKey(Slot, on_delete=models.CASCADE)
    booked_at = models.DateTimeField(auto_now_add=True)
    auto_assigned = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.teacher} booked {self.slot}"


class Notification(models.Model):
    ACTIVITY_CHOICES = [
        ('booked', 'Slot Booked'),
        ('cancelled', 'Slot Cancelled'),
        ('auto_assigned', 'Auto Assigned'),
    ]
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name='notifications')
    activity = models.CharField(max_length=20, choices=ACTIVITY_CHOICES)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.teacher} - {self.activity} - {self.created_at}"


class TeacherUnavailability(models.Model):
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE)
    date = models.DateField()

    class Meta:
        verbose_name = "Unavailable Date"
        verbose_name_plural = "Unavailable Dates"
        unique_together = ('teacher', 'date')

    def __str__(self):
        return f"{self.teacher} - {self.date}"

class DeputySuperintendent(models.Model):
    teacher = models.OneToOneField(Teacher, on_delete=models.CASCADE, related_name='deputy')
    assigned_at = models.DateTimeField(auto_now_add=True)
    assigned_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"Deputy: {self.teacher.full_name}"

    class Meta:
        verbose_name = "Deputy Superintendent"
        verbose_name_plural = "Deputy Superintendents"

class SwapRequest(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),    # target accepted, waiting admin
        ('approved', 'Approved'),    # admin approved, swap done
        ('rejected', 'Rejected'),    # target or admin rejected
        ('cancelled', 'Cancelled'),  # requester cancelled
    ]

    requester = models.ForeignKey(
        Teacher, on_delete=models.CASCADE, related_name='swap_requests_sent'
    )
    requester_booking = models.ForeignKey(
        Booking, on_delete=models.CASCADE, related_name='swap_as_requester'
    )
    target = models.ForeignKey(
        Teacher, on_delete=models.CASCADE, related_name='swap_requests_received'
    )
    target_booking = models.ForeignKey(
        Booking, on_delete=models.CASCADE, related_name='swap_as_target'
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.requester} ↔ {self.target} ({self.status})"

    class Meta:
        ordering = ['-created_at']