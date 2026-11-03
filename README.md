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

- **实时数据同步** — 一键从 Open-Meteo 拉取最新海温数据（首次含 400 天历史基线 + 7 天逐时预报）
- **五维预警引擎** — 高温 / 低温 / 日升幅 / 周升幅 / 同比异常 五维检测
- **综合评分与预警报告** — 加权评分（0–100）自动判定风险等级并生成文字报告
- **可视化仪表盘** — Leaflet 地图标记 + ECharts 趋势图 + 告警面板
- **智能分析** — 一键执行全站评估、生成报告并入库留存

---

## 技术栈

| 层次 | 技术 |
|------|------|
| 后端 | Python · Flask · SQLite |
| 前端 | HTML5 · Leaflet · ECharts 5（已本地化到 `static/vendor/`） |
| 数据源 | Open-Meteo Marine API |
| 部署 | Render (gunicorn) |
| 版本控制 | Git · GitHub |

---

## 项目结构

```
ocean-temp-monitor/
├── app.py               # 应用组装层：路由 + 后台任务
├── config.py            # 全局配置：站点/阈值/评分权重/数据源
├── database.py          # 数据库访问层：连接 + 建表 + 迁移
├── sync_service.py      # 数据同步服务：Open-Meteo 拉取 + 落库
├── alert_engine.py      # 预警引擎：五维检测 + 综合评分
├── report_service.py    # 报告生成：汇总 + 文字结论 + 入库
├── templates/
│   └── index.html       # 前端仪表盘
├── static/vendor/       # ECharts / Leaflet 本地库（离线可用）
├── requirements.txt
└── README.md
```

---

## 本地运行

```bash
# 1. 创建虚拟环境（可选但推荐）
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动服务
python app.py

# 4. 打开浏览器
# http://localhost:5000
```

启动后首次点击「同步最新数据」会拉取历史基线（约 400 天逐时数据，8 个站点，视网络需要 1–3 分钟），之后再点「执行智能分析」即可生成预警报告。

---

## 预警评分模型

五个维度各占 20 分权重，满分 100：

| 维度 | 阈值 | 说明 |
|------|------|------|
| 高温异常 | ≥ 30℃ | 超过阈值按超幅打折计分 |
| 低温异常 | ≤ 15℃ | 低于阈值按差额打折计分 |
| 日升幅 | ≥ 2℃ | 24 小时升温 |
| 周升幅 | ≥ 3℃ | 7 天升温 |
| 同比异常 | ≥ 2℃ | 与去年同期温差 |

总分 → 风险等级：`≥80 正常 / ≥60 关注 / ≥40 预警 / <40 严重`。

---

## 生产部署

项目配置了 `Procfile`，推送至 GitHub 后 Render 自动部署：

```
web: gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 120
```

---

**© 2026 OceanTempGuard**
