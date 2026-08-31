# Isolated fixture only: no application config, runtime, QQ or secrets copied.
FROM python:3.13-slim
WORKDIR /test
RUN python -m pip install --no-cache-dir pytest==9.0.3 uvicorn==0.45.0
COPY bot.py ./bot.py
COPY src/common/shutdown.py src/common/process_runner.py ./src/common/
COPY pytests/startup_test/ ./pytests/startup_test/
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python", "-m", "pytests.startup_test.docker_shutdown_fixture"]
