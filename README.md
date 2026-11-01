# 海洋温度异常预警监测平台

**辽宁理工学院 · 2026年智慧海洋科技创新大赛**

---

## 项目简介

基于 Open-Meteo Marine API 实时获取中国近海 8 个关键站点的海表温度（SST）数据，结合趋势分析、同比温差和多级预警引擎，实现海洋温度异常的实时监测与智能预警。

### 监测站点

| 站点 | 坐标 |
|------|------|
| 大连海域 | 38.9°N, 121.6°E |
| 青岛海域 | 36.1°N, 120.4°E |
| 连云港海域 | 34.6°N, 119.4°E |
| 舟山海域 | 29.9°N, 122.2°E |
| 厦门海域 | 24.5°N, 118.1°E |
| 珠江口海域 | 22.1°N, 113.8°E |
| 三亚海域 | 18.2°N, 109.5°E |
| 西沙海域 | 16.8°N, 112.3°E |

### 核心功能

- **实时数据同步** — 一键从 Open-Meteo 拉取最新海温预报数据（7天逐时）
- **多级预警引擎** — 高温/低温/日升幅/周升幅/同比异常 五维检测
- **可视化仪表盘** — Leaflet 地图标记 + ECharts 趋势图 + 告警面板
- **智能分析** — 综合评分自动生成预警报告
- **云部署** — Render.com 免费托管，公网可访问

---

## 技术栈

| 层次 | 技术 |
|------|------|
| 后端 | Python · Flask · SQLite |
| 前端 | HTML5 · Leaflet · ECharts 5 |
| 数据源 | Open-Meteo Marine API |
| 部署 | Render (gunicorn) |
| 版本控制 | Git · GitHub |

---

## 本地运行

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动服务
python app.py

# 3. 打开浏览器
# http://localhost:5000
```

## 生产部署

项目配置了 `Procfile`，推送至 GitHub 后 Render 自动部署：

```
web: gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 120
```

---

**© 2026 OceanTempGuard**