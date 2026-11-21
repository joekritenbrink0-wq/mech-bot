# Built on linux/amd64 by the scorer. Standard library only: nothing is downloaded but the pinned base image.
# The match runs it with no network, a read-only root (writable /tmp), 1 CPU, 512 MB, uid 65534, map at /arena.
FROM python:3.13.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /bot
COPY mechfight.py world.py nav.py bot.py conceal.bin ./
# Compile now: the disk is read-only at match time.
RUN python3 -m compileall -q /bot
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
