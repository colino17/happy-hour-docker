# Happy Hour
A simple docker container that pulls local business hours from OpenStreetMap and exposes rest endpoints to Home Assistant
## Docker Compose
```yaml
services:
  happy-hour:
    container_name: happy-hour
    image: ghcr.io/colino17/happy-hour-docker:latest
    restart: always
    volumes:
      - /path/to/data:/data
    environment:
      TZ=America/Moncton
      LOCATIONS=nova-scotia,new-brunswick,ontario
      DEFAULT_LAT=47.039556
      DEFAULT_LONG=-63.270816
      COVERAGE_LABEL=area name spoken in answers
      DEFAULT_LAT: ${DEFAULT_LAT:-}
      DEFAULT_LON: ${DEFAULT_LON:-}
      REFRESH_SECONDS=86400
    ports:
      - "8090:8090"
```
