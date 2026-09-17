# lightning-pipeline

三源闪电融合的**独立测试仓库**。先在 GitHub Actions 上跑通、验证，确认无误后再移植回
`polar_plus` 的生产管线（Azure Container Apps Job）。

替换的对象是现在 `polar_plus/fetch_storms.py` 用的单一第三方 Blitzortung 镜像。

---

## 为什么换

实测当前 Blitzortung 源的地理分布（某一小时的 20,570 次雷击）：

| 纬度带 | 经度带 | 占比 |
|---|---|---:|
| 30–60N | 0–30E（欧洲/地中海）| **57.0%** |
| 0–30N | 90–60W（中美/加勒比）| 13.8% |
| 30–60N | 90–60W（美国东部）| 9.5% |
| 0–30N | 120–90W（墨西哥/东太平洋）| 7.5% |
| 30–60N | 120–90W（美国西部）| 6.1% |
| 30–60N | 90–120E（亚洲）| 0.16% |
| — | 南半球合计 | **0.0%** |

93% 集中在欧洲+北美。亚太只有 2.5%（另一个小时的样本）。

---

## 三个源

### 1. NOAA GOES GLM — 匿名免费，无凭证

| 项 | 实测值 |
|---|---|
| 桶 | `noaa-goes19`（East 75.2°W）、`noaa-goes18`（West 137.2°W），S3 us-east-1 |
| 认证 | **完全匿名** |
| 路径 | `GLM-L2-LCFA/YYYY/DDD/HH/OR_GLM-L2-LCFA_G{18,19}_s…_e…_c….nc` |
| 节奏 | 20 秒/文件，180 个/小时/星 |
| 体积 | G19 约 380 KB、G18 约 230 KB → 双星约 110 MB/小时 |
| 延迟 | 窗口结束 +10~30 秒 |
| 变量 | `flash_lat`、`flash_lon`、`flash_quality_flag`（0=可用）|
| 视场 | G18 `158.8°E…72.8°W`、G19 `139.4°W…11.0°W`，纬度 ±57.6° |
| 许可 | 公有领域 |

### 2. EUMETSAT MTG-I1 LI — 免费，需注册

| 项 | 实测值 |
|---|---|
| 集合 | `EO:EUM:DAT:0691`（LI L2 LFL，闪击）|
| 检索 | OpenSearch **匿名可读** |
| 下载 | **需要 OAuth2 Bearer token** |
| 节奏 | 10 分钟/文件，144 个/天 |
| 体积 | 513 KB/个 → 约 3 MB/小时 |
| 延迟 | 约 40 秒 |
| 覆盖 | 全圆盘（0°），约 `81°W…81°E` |
| flash 定义 | 与 GLM 一致（330 ms / 16.5 km）|

### 3. Blitzortung — 第三方 JSON-RPC 镜像，只保留亚太

`http://bo-service.tryb.de/`（wuan/bo-android 项目维护）。

**实测：服务不支持按经纬度取数。**

| 方法 | 范围 | 第二参数的作用 |
|---|---|---|
| `get_global_strikes_grid(win, base, offset, thr)` | 全球，响应约 64 KB | 网格基准（分辨率）|
| `get_strikes_grid(win, base)` | **硬编码欧洲** lon −25…57 / lat 27…72 | 只有分辨率 |

扫过 `base` ∈ {3,5,10,50,500,5000,9999,10000,10001,20000,50000,100000,1000000}，
区域范围始终不变。所以亚太只能**客户端过滤**——全局响应只有 64 KB，成本可忽略。

需要的请求头（照搬 `polar_plus/fetch_storms.py`）：

```python
{"Content-Type": "text/json", "User-Agent": "bo-android-170"}
```

---

## 覆盖分区

```
Zone SAT-W  : GLM GOES-18    158.8°E → 72.8°W    lat ±57.6°
Zone SAT-E  : GLM GOES-19    139.4°W → 11.0°W    lat ±57.6°
Zone LI     : MTG LI (0°)     81°W  → 81°E       全圆盘
Zone BLITZ  : 亚太 lon 70…180, lat −55…60
```

卫星并集 = `158.8°E → 11.0°W`。全球只剩两个盲区：

1. **亚太带 `81°E…158.8°E`** —— 按你的决定，由 Blitzortung 覆盖
2. **高纬带 `|lat| > 57.6°`** —— 目前不覆盖（GLM 无数据、LI 只到中高纬）

---

## 融合算法

**不做全局随机抽样。** 现有实现按雷击次数加权抽样，会让欧洲（占 57%）淹没亚太（占 2.5%）。

```
1. 所有源的点归入 GRID_DEG × GRID_DEG 的格子（默认 1°）
2. 每个格子最多保留 POINTS_PER_CELL 个点（默认 3）
3. 格子按活跃度（权重和）降序，依次取点直到 MAX_POINTS
```

因为每格上限相同，欧洲再密也只能占满自己的格子数。**保证的是地理代表性，不是按雷击次数还原真实密度**——对一个"哪里有雷暴"的可视化来说，前者才是要的。

### 输出契约

- `storms.json` —— **裸数组** `[{"lat":..,"lng":..}]`，因为 App 侧 `StormProtos.fromJson`
  用的是 `new JSONArray(...)`。**不能为了加元数据破坏这个契约。**
- `storms_meta.json` —— 诊断信息（各源点数、格子数、降级情况、时间窗）

---

## 降级

| 故障 | 行为 |
|---|---|
| GOES S3 不可达 | 少西半球，其余正常 |
| EUMETSAT 认证/下载失败 | 少欧洲/非洲/印度洋，其余正常 |
| Blitzortung 不可达 | 少亚太，其余正常 |
| **全部失败** | **回退 40 个硬编码城市坐标**（既有设计行为，刻意保留）|

任一源失败不抛异常、不中断其他源。

---

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `LIGHT_SOURCES` | `blitzortung,glm,eumetsat` | 启用的源，便于分步验证 |
| `LIGHT_MAX_POINTS` | `2500` | 输出点上限 |
| `LIGHT_WINDOW_MINUTES` | `60` | 时间窗长度 |
| `LIGHT_POINTS_PER_CELL` | `3` | 每格上限 |
| `LIGHT_GRID_DEG` | `1.0` | 融合网格 |
| `LIGHT_BLITZ_LON_MIN/MAX` | `70` / `180` | Blitzortung 保留经度 |
| `LIGHT_BLITZ_LAT_MIN/MAX` | `-55` / `60` | Blitzortung 保留纬度 |
| `GCC_TIMESTAMP` | — | `YYYYMMDD_HHMMSS`，对齐到云图时刻 |
| `OUTPUT_DIR` | `output` | 输出目录 |
| `EUMETSAT_CONSUMER_KEY` | — | 见下 |
| `EUMETSAT_CONSUMER_SECRET` | — | 见下 |

---

## EUMETSAT 接入（需要人工做的部分）

1. **注册账号** — <https://user.eumetsat.int/>，免费，邮箱验证。个人邮箱可以。
2. **生成 API 密钥** — <https://api.eumetsat.int/api-key/>（需登录）→ 创建 API key
   → 得到 **Consumer Key** 和 **Consumer Secret**（Secret 只显示一次，立刻保存）。
3. **确认数据许可** — <https://data.eumetsat.int/> 登录后搜索 `EO:EUM:DAT:0691`，
   如有 Subscribe / 接受条款按钮就点一下。
4. **加进 GitHub Secrets** — Settings → Secrets and variables → Actions：
   - `EUMETSAT_CONSUMER_KEY`
   - `EUMETSAT_CONSUMER_SECRET`

不需要信用卡、机构邮箱、审批、纸质协议，也不需要 EUMETCast 接收站。

### 实测踩到的三个坑

**1. `links` 在 `properties` 里面，不在顶层**

```
{"properties": {..., "links": {"type": "Links", "data": [{"href": ...}]}}}
```

**2. 经纬度是 packed 的，h5py 不会自动解包**

```
latitude  : int16, scale_factor=0.0027, _FillValue=-32767
longitude : int16, scale_factor=0.0027, _FillValue=-32767
flash_filter_confidence : uint8, scale_factor=0.004, _FillValue=255
```

不解包的话纬度读成整数、经度会出现 8436 这种荒唐值。代码里 `_read_packed()`
统一处理 scale_factor / add_offset / _FillValue。

**3. `flash_filter_confidence` 是二值的，必须过滤**

实测一个 10 分钟全圆盘产品：

```
置信度 < 0.1 : 30,654 个 (90.3%)   ← 未通过滤波器
置信度 ≈ 1.0 :  3,302 个 ( 9.7%)   ← 通过
中间值       :      0
```

不过滤的话是 56 次/秒，而全球平均只有约 44 次/秒 —— 明显偏高。
过滤后 5.5 次/秒，与 GOES GLM 双星的 7 次/秒量级一致。
阈值由 `EUMETSAT_MIN_CONFIDENCE` 控制，默认 **0.5**。

### 产品结构

下载得到 ZIP，内含：

| 成员 | 说明 |
|---|---|
| `…CHK-BODY…nc` | **数据本体**（要解析的） |
| `…CHK-TRAIL…nc` | trailer，含历史块列表 |
| `manifest.xml` / `EOPMetadata.xml` | 元数据 |
| `quicklooks/*` | 预览图 |

注意按 `BODY` 而不是按 `.nc` 后缀挑成员 —— namelist 里 TRAIL 排在 BODY 前面。

认证流程（代码已实现）：

```
POST https://api.eumetsat.int/token
Authorization: Basic base64(<key>:<secret>)
Content-Type: application/x-www-form-urlencoded
body: grant_type=client_credentials
→ {"access_token": "...", "expires_in": 3600}
```

---

## 本地运行

```bash
pip install -r requirements.txt

# 只跑 Blitzortung（快，无需凭证）
LIGHT_SOURCES=blitzortung OUTPUT_DIR=/tmp/lp python -m light.pipeline

# 加 GOES GLM（需要下载约 110 MB/小时）
LIGHT_SOURCES=blitzortung,glm LIGHT_WINDOW_MINUTES=10 OUTPUT_DIR=/tmp/lp \
  python -m light.pipeline

# 全开（需要 EUMETSAT 密钥）
EUMETSAT_CONSUMER_KEY=... EUMETSAT_CONSUMER_SECRET=... \
  LIGHT_SOURCES=blitzortung,glm,eumetsat OUTLIER=... python -m light.pipeline

# 对齐到云图时刻
GCC_TIMESTAMP=20260917_140000 python -m light.pipeline
```

## GitHub Actions

- `lightning.yml` —— 每 2 小时 :48 定时跑，也支持 `workflow_dispatch`（可指定
  `sources` / `window_minutes` / `max_points`）。结果传成 artifact。

**分步验证顺序**（不要一次全开，否则出问题分不清是哪个源）：

1. `sources=blitzortung` —— 确认过滤后亚太点数和分布合理
2. `sources=blitzortung,glm` —— 看西半球的增益
3. `sources=blitzortung,glm,eumetsat` —— 全开

---

## 状态

- [x] Blitzortung 源 + 亚太过滤
- [x] GOES GLM 源（匿名 S3，32 路并发）
- [x] EUMETSAT LI 源（OAuth2 + ZIP + NetCDF）
- [x] 网格化融合 + 裸数组输出 + meta
- [x] 降级与回退
- [ ] EUMETSAT 实跑验证（等密钥）
- [ ] 60 分钟窗口的性能实测（本地链路慢，需在 runner 上测）
