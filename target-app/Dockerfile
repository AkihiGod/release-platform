# 靶子服务故意写得小，项目的主角是外面的发布工具
FROM python:3.12-slim

WORKDIR /app

# 先装依赖再拷代码：只改 app.py 时依赖层能吃到缓存，省得每次发布都重装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 5000
CMD ["python", "app.py"]
