FROM rust:1.85-slim@sha256:9f841bbe9e7d8e37ceb96ed907265a3a0df7f44e3737d0b100e7907a679acb36 AS rust-build
COPY rust/near.rs /near.rs
COPY rust/metadata.rs /metadata.rs
RUN rustc -O --crate-type cdylib /metadata.rs -o /libskill_hub_metadata.so
COPY rust/refs.rs /refs.rs
RUN rustc -O --crate-type cdylib /near.rs -o /libskill_hub_near.so
RUN rustc -O --crate-type cdylib /refs.rs -o /libskill_hub_refs.so

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ /app/app/
COPY --from=rust-build /libskill_hub_metadata.so /app/app/libskill_hub_metadata.so
COPY --from=rust-build /libskill_hub_near.so /app/app/libskill_hub_near.so
COPY --from=rust-build /libskill_hub_refs.so /app/app/libskill_hub_refs.so
COPY serve_core.py /app/serve_core.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8765
CMD ["python", "-B", "/app/serve_core.py"]
