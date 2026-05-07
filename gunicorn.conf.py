workers = 2
timeout = 60
accesslog = "-"
errorlog = "-"
# Импорт main.py один раз в master-процессе → воркеры форкаются с готовой БД.
# Иначе при пустой /app/data 2 воркера racят на PRAGMA journal_mode=WAL и один падает.
preload_app = True
