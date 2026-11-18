FROM rust:1.85-slim@sha256:9f841bbe9e7d8e37ceb96ed907265a3a0df7f44e3737d0b100e7907a679acb36 AS rust-build
WORKDIR /build
COPY src/host-rust/ /build/
RUN cargo test --offline && cargo build --release --offline

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
WORKDIR /app
RUN pip install --no-cache-dir PyYAML==6.0.3
COPY --from=rust-build /build/target/release/skill_hub_host /app/skill_hub_host
COPY src/hub-dashboard/ /app/
COPY deploy/skill-hub/app/ /indexer/
COPY deploy/hub-dashboard/container_entry.py /app/container_entry.py
RUN mkdir /state && chown 10001:10001 /state
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SKILL_HUB_ROOT=/hub SKILL_HUB_HOST_BIN=/app/skill_hub_host
USER 10001:10001
EXPOSE 8769
CMD ["python", "-B", "/app/container_entry.py"]
