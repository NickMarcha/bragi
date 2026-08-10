FROM python:3.13-slim

# pipewire-bin gives us pw-dump/pw-cli; wireplumber gives us wpctl.
# Bragi never runs its own PipeWire daemon - these talk to the host's
# daemon over a bind-mounted socket (see README/docker-compose.yml).
RUN apt-get update && apt-get install -y --no-install-recommends \
      pipewire-bin \
      wireplumber \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
