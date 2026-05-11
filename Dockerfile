# 선택 사항: Railway / Render / 자체 서버 등 컨테이너 배포용
# Streamlit Community Cloud 는 Dockerfile 없이 GitHub 연동만으로 배포 가능합니다.

FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8501

CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
