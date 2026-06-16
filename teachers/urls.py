from django.urls import path
from . import views
from . import report_views
 
urlpatterns = [
    path('dashboard/', views.dashboard, name='dashboard'),
    path('dashboard/data/', views.dashboard_data, name='dashboard_data'),
    path('dashboard/mark-read/', views.mark_notifications_read, name='mark_notifications_read'),
    path('book/<int:slot_id>/', views.book_slot, name='book_slot'),
    path('cancel/<int:booking_id>/', views.cancel_booking, name='cancel_booking'),
    path('exam-slots-count/<int:exam_date_id>/', views.exam_slots_count, name='exam_slots_count'),
    path('change-password/', views.change_password, name='change_password'),
    path('forgot-password/', views.forgot_password, name='forgot_password'),
    path('my-report/', views.download_my_report, name='download_my_report'),
    path('unavailability/add/', views.add_unavailability, name='add_unavailability'),
    path('unavailability/remove/<int:unavailability_id>/', views.remove_unavailability, name='remove_unavailability'),
    path('forgot-password/verify/', views.forgot_password_verify, name='forgot_password_verify'),
    path('swaps/', views.swap_page, name='swap_page'),
    path('swaps/request/', views.request_swap, name='request_swap'),
    path('swaps/respond/<int:swap_id>/<str:action>/', views.respond_swap, name='respond_swap'),
    path('swaps/cancel/<int:swap_id>/', views.cancel_swap, name='cancel_swap'),
    
    # ── Reports (staff only) ──────────────────────────────────────────────────
    path('reports/', report_views.reports_landing, name='reports_landing'),
    path('reports/by-date/', report_views.report_by_date, name='report_by_date'),
    path('reports/by-teacher/', report_views.report_by_teacher, name='report_by_teacher'),
]
 