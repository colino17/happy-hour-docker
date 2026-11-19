FROM python:3.12-slim

# ENVIRONMENT
ENV DATA_DIR="/data"
ENV TZ="America/Moncton"
ENV PORT="8090"

# SETUP APP
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

# VOLUMES
VOLUME ["/data"]

# PORTS
EXPOSE 8090

CMD ["python", "main.py"]
