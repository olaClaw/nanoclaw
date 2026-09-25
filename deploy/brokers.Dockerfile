# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
FROM python:3.12-slim
WORKDIR /app
COPY .claude/skills/add-infomaniak-mail-readonly/infomaniak_mail_broker.py ./infomaniak-mail.py
COPY deploy/nextcloud-calendar-broker.py ./nextcloud-calendar.py
ARG SOURCE_REVISION
ARG SOURCE_TREE
LABEL org.opencontainers.image.revision="${SOURCE_REVISION}"
LABEL org.olaclaw.source.tree="${SOURCE_TREE}"
USER 1000:1000
