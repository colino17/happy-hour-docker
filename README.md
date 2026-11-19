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
      - TZ=America/Moncton
      - LOCATIONS=nova-scotia,new-brunswick,ontario
      - DEFAULT_LAT=47.039556
      - DEFAULT_LON=-63.270816
      - COVERAGE_LABEL=area name spoken in answers
      - REFRESH_SECONDS=86400
    ports:
      - 8090:8090
```
## Home Assistant Rest Sensor
```yaml
rest_command:
  happy_hour:
    url: "http://192.168.1.10:8090/answer?q={{ q | urlencode }}{% if c %}&city={{ c | urlencode }}{% endif %}"
    method: GET
    timeout: 10
```
## Home Assistant Voice Commands
```yaml
- alias: Voice Business Hours
  id: voice_business_hours
  triggers:
    # LOCAL BUSINESSES
    - trigger: conversation
      id: voice_business_hours_local
      command:
        - "(is|when|what time) [does] [the] {business} (open|close) [today|now]"
    # BUSINESSES IN SPECIFIC CITIES
    - trigger: conversation
      id: voice_business_hours_city
      command:
        - "(is|when|what time) [does] [the] {business} in {city} (open|close) [today]"
  actions:
    - choose:
        # LOCAL BUSINESSES
        - conditions:
            - condition: trigger
              id: voice_business_hours_local
          sequence:
            - action: rest_command.happy_hour
              data:
                q: "{{ trigger.slots.business }}"
                c: ""
              response_variable: hours
            - set_conversation_response: "{{ hours.content.speech if hours.status == 200 else 'Sorry, I could not reach the business hours service.' }}"
        # BUSINESSES IN SPECIFIC CITIES
        - conditions:
            - condition: trigger
              id: voice_business_hours_city
          sequence:
            - action: rest_command.happy_hour
              data:
                q: "{{ trigger.slots.business }}"
                c: "{{ trigger.slots.city }}"
              response_variable: hours
            - set_conversation_response: "{{ hours.content.speech if hours.status == 200 else 'Sorry, I could not reach the business hours service.' }}"
```
## Disclosure
Muse and Claude were heavily used to assist the development of the subject code. Gemini was also used as a secondary code auditing tool.
