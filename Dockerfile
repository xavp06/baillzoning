FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY service.py bus_model.py bus_profile.py bus_state.py commands.py emulator.py \
     ha_discovery.py intesis_errors.py modbus_rtu.py rtu_stream.py ./
COPY profiles/ ./profiles/

RUN mkdir -p /app/data

CMD ["python", "service.py"]
