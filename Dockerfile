FROM python:3.12-slim

# ENVIRONMENT
ENV DATA_DIR="/data"
ENV TZ="America/Moncton"
ENV PORT="8090"

# SETUP APP
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1
RUN rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

# VOLUMES
VOLUME ["/data"]

# PORTS
EXPOSE 8090

# HEALTHCHECK
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health')"

# MAIN COMMAND
CMD ["python", "main.py"]
