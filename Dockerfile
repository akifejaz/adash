FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY dashboard ./dashboard

EXPOSE 8765
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "dashboard.server:app", "--host", "0.0.0.0", "--port", "8765"]
