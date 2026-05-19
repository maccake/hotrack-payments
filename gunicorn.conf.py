workers = 2
timeout = 60
accesslog = "-"
errorlog = "-"
# Импорт main.py один раз в master-процессе → воркеры форкаются с готовой БД.
# Иначе при пустой /app/data 2 воркера racят на PRAGMA journal_mode=WAL и один падает.
preload_app = True


def post_fork(server, worker):
    # preload_app импортирует main.py в master-процессе; фоновые потоки стартуем только после fork.
    from main import start_delivery_worker, start_health_monitor, start_report_scheduler, start_s3_backup_worker

    start_delivery_worker()
    start_s3_backup_worker()
    # Только один воркер шлёт отчёты — _report_sent_dates in-memory дедуп предотвращает дубли,
    # но стартуем во всех на случай если первый упадёт.
    start_report_scheduler()
    start_health_monitor()
