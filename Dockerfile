# ParsMon worker — фоновый процесс, опрашивает Telegram. HTTP нет.
# Зависимостей нет (только стандартная библиотека), поэтому образ минимальный.
FROM python:3.12-slim

# Логи сразу во вывод (без буфера) — чтобы было видно в логах DO в реальном времени.
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY monitor.py .

# Непрерывный режим (то же, что делал systemd: ExecStart … monitor.py --loop)
CMD ["python", "monitor.py", "--loop"]
