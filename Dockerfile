FROM python:3.12-slim
WORKDIR /app
COPY service/requirements.txt service/requirements.txt
RUN pip install --no-cache-dir -r service/requirements.txt gunicorn
COPY . .
ENV PORT=8080
EXPOSE 8080
# 워커 1개: SQLite 파일 하나를 쓰고, AI 에이전트 호출이 길 수 있어 타임아웃을 넉넉히 둔다
CMD ["sh", "-c", "gunicorn --chdir service --workers 1 --threads 4 --timeout 300 --bind 0.0.0.0:${PORT} 'app:create_app()'"]
