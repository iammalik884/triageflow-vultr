# Sandbox image: the ONLY place model-written code ever runs.
# No network at runtime (--network none), non-root, read-only root fs.
FROM python:3.12-slim
RUN pip install --no-cache-dir pandas==2.2.* numpy==2.* matplotlib==3.9.* \
 && rm -rf /root/.cache
USER 65534:65534
WORKDIR /work
CMD ["python3", "-I", "/work/main.py"]
