FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PORT=8000 HOST=0.0.0.0
COPY . .
RUN mkdir -p /app/data && chown -R 10001:10001 /app/data
USER 10001:10001
EXPOSE 8000
CMD ["python", "manage.py", "serve", "--host", "0.0.0.0", "--port", "8000"]
