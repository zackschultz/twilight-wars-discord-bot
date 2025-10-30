FROM python:3.14-alpine

# Tool dependencies to build python wheels. If the image ends up being too
# large, it may need to be split into a build and run image.
RUN apk add build-base

# Assumes context is project root

ENV SERVICE_NAME="twilight-wars-bot"
ENV APP_ROOT="/opt/$SERVICE_NAME" \
    STORAGE_PATH="/var/$SERVICE_NAME" \
    PYTHONPATH="/opt/$SERVICE_NAME"

COPY requirements.txt /etc/twilight-wars-bot-requirements.txt
COPY src/ $APP_ROOT

RUN pip install --root-user-action ignore --upgrade pip && \
    pip install --root-user-action ignore --requirement /etc/twilight-wars-bot-requirements.txt

RUN addgroup --gid 1001 --system $SERVICE_NAME && \
    adduser -G $SERVICE_NAME --shell /bin/false --disabled-password -H --uid 1001 $SERVICE_NAME && \
    mkdir $STORAGE_PATH && \
    chown $SERVICE_NAME:$SERVICE_NAME $STORAGE_PATH

# Additionl environment vars that should be injected
ENV BOT_APPLICATION_ID="" \
    BOT_APPLICATION_SECRET=""

USER $SERVICE_NAME

CMD ["python", "-m", "tw_bot"]
