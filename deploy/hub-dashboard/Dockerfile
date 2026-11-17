FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
WORKDIR /app
RUN pip install --no-cache-dir PyYAML==6.0.3
COPY src/hub-dashboard/ /app/
COPY deploy/skill-hub/app/ /indexer/
COPY deploy/hub-dashboard/container_entry.py /app/container_entry.py
RUN mkdir /state && chown 10001:10001 /state
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SKILL_HUB_ROOT=/hub
USER 10001:10001
EXPOSE 8769
CMD ["python", "-B", "/app/container_entry.py"]
