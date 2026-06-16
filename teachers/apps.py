from django.apps import AppConfig
import logging

logger = logging.getLogger(__name__)

class TeachersConfig(AppConfig):
    name = 'teachers'

    def ready(self):
        import os
        # Prevent running twice in dev (Django starts twice with autoreload)
        if os.environ.get('RUN_MAIN') != 'true':
            return
            
        try:
            from . import scheduler
            scheduler.start()
            logger.info("Scheduler started successfully.")
        except Exception as e:
            logger.error(f"Scheduler failed to start: {e}")