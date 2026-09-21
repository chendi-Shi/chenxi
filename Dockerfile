FROM python:3.12-slim
RUN useradd -m -u 1000 appuser
WORKDIR /home/appuser/app
COPY pyproject.toml README.md requirements-dev.lock ./
COPY src ./src
RUN pip install --no-cache-dir -c requirements-dev.lock ".[web]"
USER appuser
ENV PYTHONUNBUFFERED=1 DAILY_DATA_DIR=/home/appuser/data
EXPOSE 7860
CMD ["uvicorn", "research_agent.space_app:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "1", "--no-access-log"]
