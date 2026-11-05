# 开发记录

## 问题排查记录

### 2026-11-04 气象数据不正确（取到 8 小时前的时刻 + 线上 429）

- **现象**：用户反馈"气象数据不正确"。线上站点「多源数据」页气象全为空，且即便有值也偏离当前时刻。
- **影响范围**：`/api/sources/<id>` 的气象要素（气温 / 风速 / 风向 / 气压 / 降水 / 云量 / 湿度）与海洋要素的"当前值"；`suitability` 综合适宜度依赖 `wind_speed.now`，一并受影响。监测总览、预警引擎、7 天预报不受影响（走 SQLite，时间轴由 `sync_service` 生成）。
- **复现步骤**：
  1. `GET https://ocean-temp-monitor-1-1.onrender.com/api/sources/1`
  2. 观察 `timeline.times[0]` 与 `weather.ok`
  3. 实测结果：`timeline[0] = 2026-11-04T15:00`，而当时上海墙钟为 `23:00`；`weather.ok = False`
- **定位过程**（逐层排除）：
  1. **数据源层排除**：本机直连 `api.open-meteo.com/v1/forecast`（38.9,121.6）返回 `temperature_2m=15.6℃ / wind_speed_10m=18.4km/h / humidity=80%`，单位与量级均正常 → 上游数据本身没问题。
  2. **时间轴对齐假设排除**：怀疑 `app.py` 用 marine 的 `idx` 去索引 weather 数组会错位。实测两 API 均返回 48 个点、`time` 完全相同（`2026-11-04T00:00` ~ `11-05T23:00`）→ 该假设不成立，`idx` 共用是安全的。
  3. **定位到应用层**：`_pick_current_index()` 用无时区的 `datetime.now()` 去匹配按 `Asia/Shanghai` 标注的 `time`。Render 容器进程时区为 UTC，故匹配到 UTC 墙钟对应的小时槽。
  4. **铁证**：线上 `fetched_at=2026-11-04T15:53`（UTC 墙钟）与 `timeline[0]=2026-11-04T15:00` 完全吻合，而上海当时是 23:00 → 偏差正好 8 小时。
  5. **第二个独立缺陷**：连续 3 次（间隔 20s）请求线上 `weather.ok=False`，error 原文为 `429 Client Error: Too Many Requests for url: https://api.open-meteo.com/v1/forecast?...`；同参数本机直连 2 次均成功（1.38s / 1.91s）→ 限流针对 Render 出口 IP，非代码参数问题。
  6. **放大机制**：`_cache_set` 仅在 `ok` 时写入（`app.py` L580/L588），失败结果不入缓存，导致每次访问都重新打 API，持续撞 429。
- **根因**：
  1. `_pick_current_index()` 跨时区比较——拿服务器本地（UTC）墙钟去匹配数据自带的 +8 时区时刻标签，取到 8 小时前的槽位。
  2. Render 出口 IP 被 open-meteo 限流（429），且失败不缓存使请求量无法衰减。
- **修复**（本次只改根因 ①，遵守"一次只改一处"）：
  - `app.py`：`_pick_current_index(times, utc_offset_seconds=None)` 改为按数据自身时区取当前时刻；偏移优先取 API 响应的 `utc_offset_seconds`，缺失时回退 `config.TIMEZONE_UTC_OFFSET_SECONDS`。
  - `app.py`：`fetch_marine_live` / `fetch_weather_live` 返回体增加 `utc_offset_seconds`（透传 API 字段）。
  - `app.py`：`/api/sources/<id>` 调用点传入偏移。
  - `config.py`：新增 `TIMEZONE_UTC_OFFSET_SECONDS = 8 * 3600`。
  - 未引入新依赖，未改动数据层与前端。
- **验证**：
  1. `python -m py_compile app.py config.py` → 通过。
  2. **单元级（模拟 UTC 服务器）**：monkeypatch `datetime.now()` 返回 UTC 墙钟，用真实 API 数据跑——旧逻辑 `idx=15 → 2026-11-04T15:00`，新逻辑 `idx=23 → 2026-11-04T23:00`，偏差 8 小时；传 API 偏移与回退配置常量两条路径均命中上海当前小时 → PASS。
  3. **端到端（本地服务）**：`GET /api/sources/1` 返回 `timeline[0]=2026-11-04T23:00`、`weather.ok=True`、`air_temp.now=15.6℃ / wind_speed.now=18.4km/h / humidity=80% / pressure=1014.6hPa`；station 5 同样正常。
  4. 注：本地机器时区即 Asia/Shanghai，故本地无法复现 8 小时偏差，本地验证只证明"未回归"；偏差的证据来自线上 `timeline[0]=15:00` 与 monkeypatch 测试。
- **遗留**：
  1. **根因 ② 未修**（429 属外部限流，需决策后再动，见下）。候选方案：
     - 失败结果短 TTL 负缓存（如 60s），避免每请求都打上游；
     - 加退避重试（`fetch_fishery_stats` 已有 2 次重试范式，可对齐）；
     - 气象数据改由 `/api/refresh` 后台同步写入 SQLite，前端读库而非实时打 API；
     - 或为 Render 固定出口 IP / 升级实例。
  2. `templates/index.html` L897/L907 静态卡片写死"已接入"徽章，是说明文案而非实时状态；实时状态由 `badgeWeather` 按 `w.ok` 渲染（L1684），逻辑正确。若希望卡片也反映实时状态，需另行改造。
  3. 本次改动尚未部署到 Render，线上仍是旧行为。
