# Scripted SIP test bot (extension 1099) used by tools/acceptance.py. Runs on the Pi next to Asterisk.
FROM alpine:3.22
RUN apk add --no-cache py3-pjsua
WORKDIR /work
COPY bot.py /app/bot.py
ENTRYPOINT ["python3", "-u", "/app/bot.py"]
