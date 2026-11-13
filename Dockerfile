FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py proxy.py estimator.py db.py notify.py demo_seed.py prices.json ./
COPY static/ static/

# 数据落到挂载卷,容器重建不丢账单;云环境不需要自动开浏览器
ENV DSCD_NO_BROWSER=1 \
    DSCD_DATA_DIR=/data

VOLUME ["/data"]
EXPOSE 8787

CMD ["python", "main.py"]
