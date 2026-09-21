# syntax=docker/dockerfile:1
FROM python:3.12-slim
RUN useradd -m -u 1000 appuser
WORKDIR /home/appuser/app
ADD --checksum=sha256:47733c796faa03d1d9d5a5f23ec9c7c3490e09ee6bfcc7768b144c3d49ea522d https://codeload.github.com/chendi-Shi/chenxi/tar.gz/f81a999c6b605128c1fe7888891fc9cfaed1aa86 /tmp/research.tar.gz
RUN mkdir /tmp/research && tar -xzf /tmp/research.tar.gz -C /tmp/research --strip-components=1 && pip install --no-cache-dir -c /tmp/research/requirements-dev.lock "/tmp/research[web]"
USER appuser
ENV PYTHONUNBUFFERED=1 DAILY_DATA_DIR=/home/appuser/data
EXPOSE 7860
CMD ["uvicorn", "research_agent.space_app:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "1", "--no-access-log"]
